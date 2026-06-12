# Tools Passthrough — Design Document

**Date:** 2026-06-12
**Status:** Implemented
**Author:** Alexander Melnik
**Version:** v3.4

---

## Контекст и цель

Gateway проксирует chat-запросы к upstream LLM. До этой версии поддерживались только текстовые сообщения (`role: user/assistant/system`). Многие клиенты — agent frameworks, MCP-адаптеры, coding assistants — используют OpenAI `tools` API для структурированного вызова функций.

Цель: сделать gateway прозрачным для `tools`, чтобы клиенты передавали свои схемы инструментов и сами обрабатывали их вызов. Gateway не запускает агентный цикл.

---

## Архитектурное решение: логика инструментов на стороне клиента

Ключевой вопрос — кто должен знать об инструментах и их схемах.

**Вариант A: Gateway регистрирует инструменты**
Gateway хранит реестр известных инструментов (например, Sentry MCP, внутренние API) и инжектирует их в запросы на основе идентификатора клиента или конфига. Удобно для клиента, но: gateway превращается в монолит, каждый новый инструмент требует деплоя gateway, схемы инструментов расходятся с реальными реализациями.

**Вариант B: Клиент передаёт схемы инструментов с каждым запросом (выбрано)**
Клиент передаёт полный OpenAI-совместимый массив `tools` в каждом запросе. Gateway проксирует его в LLM без анализа. LLM возвращает `tool_calls`; gateway возвращает их клиенту. Клиент выполняет инструмент и отправляет следующий запрос с сообщением `role: "tool"`.

Выбран вариант B. Gateway остаётся тонким прокси. Клиенты владеют своими инструментами. Добавление нового MCP-адаптера не требует изменений в gateway.

---

## Поток данных

### Не-потоковый запрос с инструментами

```
Клиент → POST /v1/chat/completions  { tools: [...], messages: [...] }
          ↓
routes.py        извлекает tools, передаёт в chat_service.complete(tools=...)
          ↓
chat_service     санитизирует сообщения, собирает ChatRequest(tools=tools)
          ↓
adapter          добавляет tools в payload к upstream, парсит tool_calls из ответа
          ↓
routes.py        если response.tool_calls:
                   finish_reason = "tool_calls"
                   message = {role: "assistant", content: null, tool_calls: [...]}
          ↓
Клиент ← { choices: [{ message: {...}, finish_reason: "tool_calls" }] }
```

### Агентный цикл на стороне клиента

Gateway не хранит состояния. После получения `tool_calls` клиент:
1. Выполняет инструмент локально
2. Отправляет новый запрос с полной историей сообщений плюс:
   - `{"role": "assistant", "tool_calls": [...]}`
   - `{"role": "tool", "tool_call_id": "...", "content": "<результат>"}`

Gateway воспринимает это как обычный многосообщенный запрос.

### Стриминг с tool_calls

Протокол OpenAI streaming передаёт `tool_calls` через `delta.tool_calls` вместо `delta.content`. Адаптер генерирует два типа чанков:

- `str` — текстовый контент (существующее поведение)
- `dict` — дельта `{"tool_calls": [...]}` когда модель вызывает инструмент

`routes.py` обрабатывает оба типа. Он также отслеживает, были ли получены дельты tool_call, чтобы правильно установить финальный `finish_reason` (`"tool_calls"` или `"stop"`).

---

## Восстановление PII в аргументах tool_call

### Проблема

Input sanitizer заменяет PII до того, как LLM его увидит:
```
"Send email to john@example.com" → "Send email to [EMAIL_ADDRESS_3f2a1b0c]"
```

LLM может вернуть placeholder в аргументах tool_call:
```json
{"function": {"name": "send_email", "arguments": "{\"to\": \"[EMAIL_ADDRESS_3f2a1b0c]\"}"}}
```

Без восстановления клиент получает placeholder вместо оригинального значения, что ломает вызов инструмента.

### Фикс для не-стриминга

После получения ответа адаптера, если `context.has_replacements()`:

```python
if response.tool_calls is not None:
    restored = json.loads(context.restore(json.dumps(response.tool_calls)))
    response = dataclasses.replace(response, tool_calls=restored)
```

Сериализуем в JSON-строку, запускаем `context.restore()` (простая замена строк), парсим обратно. Работает, так как placeholder-ы — это непрерывные строки внутри сериализованного JSON.

### Фикс для стриминга: per-index StreamingRestorer

В стриминге LLM отправляет `arguments` посимвольно в множестве SSE-чанков:

```
arguments: "{"    arguments: "\"to\": \""    arguments: "["
arguments: "EMAIL"    arguments: "_ADDRESS_3f2a1b0c]"    arguments: "\""
```

Placeholder разбит на части. Побайтовый `str.replace()` на каждом чанке его не найдёт.

**Решение:** применить ту же логику `StreamingRestorer`, что используется для текстового контента, но для каждого `tool_call index` отдельно. Каждый index получает свой экземпляр `StreamingRestorer`, который буферизует только вокруг границ `[PLACEHOLDER]`:

- Символы до `[` → пересылаются немедленно
- После `[` → буферизуются пока проверяется, может ли это быть префиксом placeholder-а
- Когда полный placeholder найден → отдаём оригинальное значение одним чанком
- Когда префикс не может совпасть → отдаём `[` немедленно и продолжаем

Это даёт настоящий стриминг: фрагменты аргументов появляются у клиента в реальном времени, задержка ограничена длиной placeholder-а (~26 символов) только вокруг самого placeholder-а.

```python
tc_restorers: dict[int, StreamingRestorer] = {}

for tc in chunk.get("tool_calls", []):
    idx = tc.get("index", 0)
    if "arguments" in tc.get("function", {}):
        if idx not in tc_restorers:
            tc_restorers[idx] = StreamingRestorer(context)
        safe = tc_restorers[idx].feed(func["arguments"])
        # yield чанк с safe-фрагментом — может быть пустым во время буферизации
```

После стрим-цикла вызывается `finalize()` на каждом restorer-е для сброса оставшегося буфера (например, `[` в конце потока).

### Что насчёт `[` и `]` в обычном тексте?

`StreamingRestorer._probe()` буферизует только когда буфер является **префиксом** зарегистрированного placeholder-а. `[category]` вызовет буферизацию на `[`, но когда придёт `c`, станет ясно, что это не может совпасть с `[EMAIL_ADDRESS_...]` → `[` отдаётся немедленно. Задержка в один чанк на `[`, ничего не теряется.

Ложное срабатывание (LLM генерирует строку, идентичную зарегистрированному placeholder-у) теоретически возможно, но крайне маловероятно: placeholder-ы используют `secrets.token_hex(4)` — случайный суффикс из 4 байт, 2³² вариантов.

---

## Изменения доменной модели

```python
@dataclass
class ChatMessage:
    role: str
    content: str | None = None        # None для assistant-сообщений только с tool_calls
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None

@dataclass
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    stream: bool = False
    tools: list[dict] | None = None   # прозрачный passthrough

@dataclass
class ChatResponse:
    model: str
    usage: dict
    content: str | None = None
    tool_calls: list[dict] | None = None
```

Все новые поля опциональны с дефолтом `None` — существующие вызывающие стороны не ломаются.

---

## Явно вне области видимости

- Выполнение инструментов на стороне gateway (нет агентного цикла)
- Санитизация `function.arguments` на входе (схемы инструментов клиента)
- Валидация схем инструментов (трактуются как opaque blobs)
- Логирование `tool_calls` в `AuditRecord`
- Проброс параметра `tool_choice` (тривиально добавить, пока не нужно)
