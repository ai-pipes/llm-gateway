# Streaming Support — Design Document

**Date:** 2026-06-10
**Status:** Implemented
**Author:** Alexander Melnik
**Version:** v3.1

---

## Контекст и цель

v3.0 поддерживает только request/response модель: `POST /v1/chat/completions` → полный JSON-ответ. Большинство реальных клиентов (SDK, chat-UI, агентские фреймворки) по умолчанию отправляют `{"stream": true}` — без поддержки они либо падают, либо ждут полный ответ, что создаёт таймауты на длинных генерациях.

Цель: добавить SSE streaming (`data: {...}\n\n`) совместимый с OpenAI API, при этом **не трогать sanitizer и audit**.

---

## Ключевое дизайн-решение: что делать с output sanitizer

Главный вопрос при стриминге — как совместить его с output sanitizer и audit, которые работают на полном тексте.

### Три варианта

**Вариант A: Buffer → SSE (pseudo-streaming)**
- Вызываем адаптер без стриминга, получаем полный ответ
- Запускаем output sanitizer как обычно
- Отдаём клиенту в SSE-формате, разбив на чанки
- Клиент не таймаутится, но настоящего TTFF нет

**Вариант B: Upstream streaming + buffer → SSE**
- Стримим от LLM, буферизуем чанки на нашей стороне
- После полного буфера — output sanitizer на полном тексте, audit
- Клиент получает SSE, но TTFF = полное время ответа LLM

**Вариант C: True end-to-end streaming (выбранный)**
- Стримим от LLM → сразу отдаём клиенту
- Output sanitizer убираем из критического пути
- Audit пишем постфактум в `finally` блоке после завершения стрима
- Настоящий TTFF

### Почему output sanitizer не нужен на ответе

Chunk-by-chunk санитайзация ненадёжна: PII-детектор (NER-модель) требует полного контекста — "Джон" и "Смит" могут прийти в разных чанках, и entity не будет найдена. Это не технический компромисс, а архитектурная невозможность.

Но главное: output sanitizer защищает от утечки PII клиенту. Имеет смысл, если **gateway знает больше, чем клиент** — например, RAG с данными всех пользователей. Если такого сценария нет, output sanitizer на ответе малополезен.

Для **audit логов** польза тоже неочевидна: если PII появляется в ответе LLM, оно попало туда из запроса клиента — который уже санитайзирован на входе. Audit хранит placeholder-версию входных сообщений, чего достаточно. Output sanitizer убран и из пути audit body logging (v3.5).

### Итог

```
Input sanitizer:  full messages (как всегда) → без изменений
Output sanitizer: не применяется к ответу LLM
Audit:            пишется в finally после завершения стрима
Клиент:           получает токены сразу — настоящий TTFF
```

---

## Архитектура

### Поток данных

**Non-streaming (без изменений):**
```
Request → input sanitize → adapter.chat() → audit.write() → JSON response
```

**Streaming:**
```
Request → input sanitize → adapter.stream_chat()
                                   │
                             chunk → yield SSE → клиент  (real-time TTFF)
                             chunk → buffer[]
                                   │
                           [DONE] / disconnect
                                   │
                         join(buffer)
                                   │
                         audit.write()  ← гарантирован через finally
```

### Что изменилось по слоям

#### `domain/adapters/base.py`

Добавлен параметр `usage_out: dict | None = None` к `stream_chat()`. Адаптер заполняет его usage-данными из последнего SSE чанка. Дефолтная реализация — fallback через `chat()`.

```python
async def stream_chat(
    self, request: ChatRequest, usage_out: dict | None = None
) -> AsyncGenerator[str, None]:
    response = await self.chat(request)
    yield response.content
```

#### `infrastructure/adapters/openai_compatible.py`

Реальная реализация `stream_chat()` через `httpx.AsyncClient.stream()`:
- Парсит SSE строки (`data: ...` префикс, `[DONE]` сентинел)
- Yield только непустой `delta.content`
- После `[DONE]` продолжает итерировать для захвата trailing usage chunk (OpenAI отдаёт usage после `[DONE]` при `stream_options: {include_usage: true}`)
- Guard против `choices[0]` = None
- Общий `_build_payload()` для `chat()` и `stream_chat()` — нет дублирования

```python
async def stream_chat(self, request, usage_out=None):
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", url, json=self._build_payload(request, stream=True)) as resp:
            resp.raise_for_status()
            stream_done = False
            async for line in resp.aiter_lines():
                if not line.startswith("data: "): continue
                payload = line[6:]
                if payload == "[DONE]":
                    stream_done = True
                    continue   # продолжаем — за [DONE] может прийти usage
                data = json.loads(payload)
                if usage_out is not None and data.get("usage"):
                    usage_out.update(data["usage"])
                if stream_done: continue
                content = data["choices"][0].get("delta", {}).get("content", "")
                if content: yield content
```

#### `application/chat_service.py`

Новый метод `complete_stream()` — async generator с `try/finally`:

- Фаза 1 (до первого yield): input sanitization + adapter lookup. Ошибки здесь (`SanitizerBlockedError`, `AdapterNotFoundError`) пишут audit и поднимают исключение до начала стрима — клиент получает HTTP 400 до коммита заголовков.
- Фаза 2 (streaming): `try/except/finally` вокруг `async for chunk in adapter.stream_chat()`. Каждый чанк немедленно yielded клиенту, параллельно добавляется в буфер.
- Фаза 3 (`finally`): гарантированно выполняется при успехе, ошибке и дисконнекте клиента (GeneratorExit). Собирает полный текст, пишет audit.

**Статусы в audit при стриминге:**

| Ситуация | `status` |
|---|---|
| Стрим завершён нормально | `success` |
| Клиент отключился до конца | `cancelled` |
| Таймаут upstream | `error` (error: "upstream_timeout") |
| Другая ошибка upstream | `error` |

`cancelled` определяется через флаг `stream_complete = False`, который ставится в `True` только после нормального завершения цикла `async for`.

Общий код sanitization и adapter lookup вынесен в `_sanitize_input()` и `_resolve_adapter()` — используется как в `complete()`, так и в `complete_stream()`.

#### `api/openai/routes.py`

При `body.get("stream")` возвращает `StreamingResponse(media_type="text/event-stream")`.

Генератор `_sse_stream()` форматирует чанки в OpenAI chunk формат:
```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"...","choices":[{"index":0,"delta":{"content":"Привет"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

Ключевые детали:
- **`[DONE]` гарантирован через `finally`** — отправляется даже при ошибке
- **Ошибки как SSE-события**: HTTP 200 уже отправлен после первого байта, статус менять нельзя. Ошибки сериализуются как `data: {"error": {...}}\n\n` перед `[DONE]`
- **`delta: {}`** в финальном чанке (не `{"content": ""}`) — по spec OpenAI
- **`model`** поле в каждом чанке

---

## Usage токены при стриминге

Известное ограничение: `complete_stream()` пишет в audit `prompt_tokens` и `completion_tokens` из `usage_out`. Если провайдер не поддерживает `stream_options: {include_usage: true}` или не отдаёт usage — в audit будут нули.

Это принятый компромисс: большинство production-провайдеров (OpenAI, Anthropic, vLLM) отдают usage в последнем чанке.

---

## Трейдофы и известные ограничения

| Аспект | Решение | Трейдоф |
|---|---|---|
| Output sanitizer на ответе | Убран полностью (v3.5) | Ответ клиенту не санитайзируется; audit body logging хранит сырой текст |
| Audit | Постфактум в `finally` | Audit не блокирует первый токен; если audit упадёт — клиент уже получил ответ |
| Usage токены | Best-effort из последнего чанка | Нули если провайдер не поддерживает |
| Модель в audit | Requested model, не resolved | vLLM/proxy могут маппить aliases — в стриминге нет response объекта |
| `temperature` и другие параметры | Не пробрасываются из HTTP body | Pre-existing gap, не добавлен в этом релизе |

---

## Обработка ошибок

### Pre-stream ошибки (до первого yield)
Возникают во время input sanitization и adapter lookup. Генератор ещё не начал yield — FastAPI ловит исключение и возвращает HTTP 400/404 как обычный JSON ответ.

### In-stream ошибки (после первого yield)
HTTP 200 уже отправлен. Ошибки сериализуются как SSE error events:

```
data: {"error":{"type":"gateway_error","code":"upstream_timeout","message":"LLM request timed out"}}

data: [DONE]
```

| Исключение | SSE code |
|---|---|
| `SanitizerBlockedError` | `sanitizer_blocked` |
| `AdapterNotFoundError` | `adapter_not_found` |
| `UpstreamTimeoutError` | `upstream_timeout` |
| `UpstreamError` | `upstream_error` |
| Любое другое | `internal_error` |

---

## Тестирование

### `tests/unit/test_adapters.py` — новые тесты

| Тест | Что проверяет |
|---|---|
| `test_stream_chat_yields_content_chunks` | чанки yielded корректно |
| `test_stream_chat_populates_usage_out` | usage заполняется из чанка с usage |
| `test_stream_chat_usage_after_done_sentinel` | usage захватывается даже если идёт после `[DONE]` |
| `test_stream_chat_skips_empty_delta` | role-only и пустые delta не yielded |
| `test_stream_chat_raises_on_http_error` | HTTP 4xx/5xx поднимает исключение |

### `tests/unit/test_chat_service.py` — новые тесты

| Тест | Что проверяет |
|---|---|
| `test_complete_stream_yields_chunks` | чанки доходят до вызывающего |
| `test_complete_stream_writes_audit_after_stream` | audit пишется после стрима |
| `test_complete_stream_no_body_logging_by_default` | `completion=None` без `log_body` |
| `test_complete_stream_body_logging_records_raw_output` | completion в audit содержит сырой текст (output sanitizer не применяется) |
| `test_complete_stream_blocked_input_raises_before_yielding` | blocked → audit + исключение до yield |
| `test_complete_stream_adapter_not_found_raises_before_yielding` | not found → audit + исключение до yield |
| `test_complete_stream_upstream_timeout_writes_audit` | timeout → `error` в audit |
| `test_complete_stream_upstream_error_writes_audit` | generic error → `error` в audit |
| `test_complete_stream_usage_from_adapter` | usage_out пробрасывается в audit |
| `test_complete_stream_cancelled_on_disconnect` | `aclose()` → `status=cancelled` в audit |

### `tests/unit/test_openai_routes.py` — новые тесты

| Тест | Что проверяет |
|---|---|
| `test_stream_returns_200_with_sse_content_type` | правильный content-type |
| `test_stream_yields_content_chunks_as_sse` | чанки в OpenAI chunk формате |
| `test_stream_final_chunk_has_finish_reason_stop` | финальный чанк с `finish_reason=stop` |
| `test_stream_ends_with_done_sentinel` | `data: [DONE]` в конце |
| `test_stream_blocked_sends_error_sse_event` | ошибки как SSE events с `error.code` |
| `test_stream_always_ends_with_done_sentinel_on_error` | `[DONE]` даже при ошибке |
| `test_non_streaming_route_unaffected_by_streaming_changes` | non-streaming путь не сломан |
