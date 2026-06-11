# DEVLOG — LLM Gateway

Хронологический журнал разработки. Здесь — ход мыслей, тупики, инсайты. Архитектурные решения с обоснованием — в `adr/`.

---

## v3.3 — Streaming PII Restoration (2026-06-12)

**No breaking changes.** `complete_stream()` теперь восстанавливает PII с настоящим TTFF — максимальная задержка равна длине самого длинного зарегистрированного placeholder-а (~26 символов).

### Проблема со стримингом

`[EMAIL_ADDRESS_3f2a1b0c]` — 26 символов. Токенизатор LLM разбивает текст на произвольные границы — placeholder может прийти как `[EMAIL`, `_ADDRESS_`, `3f2a`, `1b0c]` в четырёх отдельных SSE-чанках. Наивный `str.replace()` на каждом чанке не поймает совпадения на границах.

Очевидное решение — буферизовать весь ответ, восстановить, потом ре-стримить — меняет настоящий TTFF на фейковый. Клиент всё равно ждёт полный ответ перед получением первого токена.

### Почему look-ahead буфер — правильная форма

Структура placeholder-ов известна заранее: они начинаются с `[` и заканчиваются `]`. Удерживать нужно только байты, начиная с `[`. Всё до `[` можно передавать немедленно.

Это, по сути, потоковый поиск строки: держать ровно столько состояния, чтобы совпасть или отклонить, затем отпустить. Максимальная задержка ограничена длиной самого длинного placeholder-а — на практике всегда меньше 30 символов.

### Реализация: `StreamingRestorer._probe()`

Ключевой инсайт: мы знаем точный набор строк, которые ищем (зарегистрированные placeholder-ы). Вместо общего автомата — просто проверка префиксов по известному множеству:

1. Буфер начинается с полного зарегистрированного placeholder-а? → восстановить и отпустить.
2. Буфер является валидным префиксом какого-либо зарегистрированного placeholder-а? → ждать.
3. Ни то, ни другое? → сбросить `[` и продолжать.

Вызов `any(p.startswith(buf) for p in self._map)` в шаге 2 делает линейный обход по всем зарегистрированным placeholder-ам. Для типичного случая 1–10 placeholder-ов на запрос это незначительно. Trie сэкономил бы константный множитель; оно того не стоит.

### Аудит в стриминге

Сырые (с placeholder-ами) чанки идут в `chunks[]` для аудит-логирования тела. Restorer трансформирует только то, что yielded клиенту. Это зеркалит дизайн аудита для не-стриминга: аудит видит placeholder-ы, клиент видит оригиналы.

### `finalize()` по завершении потока

Когда поток заканчивается, в буфере может остаться частичный prefix placeholder-а (например, `[EMAIL_ADDR` если поток был прерван). `finalize()` сбрасывает его как есть. Это безопаснее, чем проглотить: клиент видит частичный токен и знает, что что-то неполное, вместо того чтобы молча потерять символы.

---

## v3.2 — PII Restoration (2026-06-12)

**No breaking changes.** Обратно-совместимо: параметр `context` по умолчанию `None`, существующие санитайзеры не изменены.

### Что сделано

Добавлен `RestorationContext` — объект на один запрос, хранящий маппинг `placeholder → original`. Когда input sanitizer заменяет PII, он регистрирует каждую замену в контексте. После ответа LLM gateway восстанавливает оригинальные значения в `response.content` перед возвратом клиенту. Клиент отправляет реальный email и получает реальный email — LLM видит только placeholder.

Только `complete()` (не streaming). Восстановление в стриминге — отдельная будущая задача.

### Подход: почему объект контекста

Альтернатива — хранить маппинг в `request.state`. Отклонили по той же причине, по которой цепочка санитайзеров была отделена от HTTP слоя: `request.state` смешивает HTTP и application concerns, делает chat service сложнее для тестирования в изоляции, и привязывает маппинг к жизненному циклу FastAPI. `RestorationContext`, созданный в начале `complete()`, имеет очевидную область видимости, чисто передаётся через цепочку санитайзеров и тривиально тестируется.

### Безопасность аудита

Запись аудита хранит **санитайзированную** (placeholder) версию ответа LLM, не восстановленную. Порядок важен:

```python
sanitized_content = response.content              # захватываем до восстановления
response = replace(response, content=ctx.restore(response.content))
await audit.write(..., completion=sanitized_content)   # placeholder, не PII
```

Если порядок инвертировать — аудит логирует сырой PII, что обнуляет смысл санитайзации.

### Два бага, найденные при live-тестировании

**1. LLM копирует пример placeholder-а буквально**

Системная инструкция содержала: *"tokens like [EMAIL_a3f7c2b1]"*. gpt-4o-mini увидел это и написал `[EMAIL_a3f7c2b1]` прямо в подпись письма — как обычную шаблонную переменную. Зарегистрированный placeholder не использовался вовсе.

Исправление: заменить конкретно выглядящий пример на очевидно ненастоящий: `[EMAIL_ADDRESS_3f2a1b0c]`, плюс добавить пометку *"These are NOT real values — do not invent, guess, or reconstruct the originals."*

Урок: всё, что в system prompt выглядит как шаблонная переменная, модель *будет* воспринимать как шаблонную переменную.

**2. Перекрывающиеся спаны Presidio искажают замену**

Presidio определил `sarah@techcorp.io` одновременно как `EMAIL_ADDRESS` (позиции 29–46, score 1.0) и как `URL` для `techcorp.io` (позиции 35–46, score 0.5). Замена справа налево обработала `URL` первым (больший start-индекс), вставила `[URL_xxxxxxxx]`, потом попыталась заменить `EMAIL_ADDRESS` по оригинальным индексам — получился `[EMAIL_ADDRESS_xxxxxxxx]a1]`.

Исправление: `_resolve_conflicts()` убирает перекрывающиеся спаны до замены, оставляя наиболее уверенный при каждом конфликте. Это повторяет то, что `AnonymizerEngine.anonymize()` делает внутри, но в context-aware пути мы обходим `anonymize()` полностью.

Баг не проявлялся в стандартном (без context) пути, потому что `AnonymizerEngine` обрабатывает конфликты внутренне. Кастомный цикл с заменой справа налево пришлось научить этому явно.

---

## v3.1 — SSE Streaming (2026-06-10)

**No breaking changes.** Полностью обратно-совместимо: запросы без `"stream": true` проходят по старому пути без изменений.

**New feature:**
- `POST /v1/chat/completions` с `"stream": true` возвращает `StreamingResponse(media_type="text/event-stream")` в формате OpenAI streaming (чанки `chat.completion.chunk` + `data: [DONE]`).
- Настоящий end-to-end streaming: токены идут клиенту сразу, без буферизации на gateway.
- `complete_stream()` в `ChatService` — async generator с `try/finally` гарантирующим запись audit при успехе, ошибке и дисконнекте клиента.
- `stream_chat()` в `OpenAICompatibleAdapter` — httpx streaming с захватом usage из trailing chunk.
- Output sanitizer убран из критического пути клиентского ответа; для audit body logging запускается постфактум на полном тексте в `finally` блоке.
- `status="cancelled"` в audit при разрыве соединения (через `stream_complete` флаг + `GeneratorExit`).
- Ошибки в stream сериализуются как SSE error events (`data: {"error": {...}}`); `data: [DONE]` гарантирован через `finally`.

**Architecture changes:**
- `BaseLLMAdapter.stream_chat()` — новый абстрактный метод с `usage_out: dict | None = None` параметром.
- `ChatService._sanitize_input()` и `_resolve_adapter()` — вынесены в приватные методы, переиспользуются в `complete()` и `complete_stream()`.
- `OpenAICompatibleAdapter._build_payload()` — вынесен из `chat()`, переиспользуется в `stream_chat()`.

---

## v3.0 — Layered Architecture + Body Logging (2026-06-09)

**Breaking changes:**
- Import paths for all base classes and implementations have changed. Old paths (`gateway.middleware.auth`, `gateway.audit.base`, `gateway.adapters.base`, `gateway.sanitizers.base`) are removed. New paths:
  - `gateway.infrastructure.auth.static_key.StaticKeyAuthProvider`
  - `gateway.infrastructure.auth.static_key.AuthMiddleware`
  - `gateway.infrastructure.auth.base.BaseAuthProvider`
  - `gateway.domain.audit.base.BaseAuditBackend`
  - `gateway.domain.adapters.base.BaseLLMAdapter`
  - `gateway.domain.sanitizers.base.BaseSanitizer`, `SanitizerChain`
  - `gateway.infrastructure.audit.stdout_backend.StdoutAuditBackend`
  - `gateway.infrastructure.audit.file_backend.FileAuditBackend`
  - `gateway.infrastructure.adapters.openai_compatible.OpenAICompatibleAdapter`
  - `gateway.infrastructure.sanitizers.pii_regex.PiiRegexSanitizer`

**Architecture changes:**
- `SanitizeMiddleware` and `AuditLogMiddleware` removed. Logic consolidated into `ChatService` (application layer).
- New layer structure: `api/openai/` → `application/` → `domain/` → `infrastructure/`.
- `request.state` no longer used as an implicit data channel between layers.

**New feature:**
- `AuditRecord` now has optional `messages: list[dict] | None` and `completion: str | None` fields.
- Opt-in body logging: `audit.body_logging.enabled: true` logs full sanitized messages and LLM completion in every audit record.

---

## 2026-06-05 — Старт проекта и исследование ландшафта

### Почему этот проект

Начинаю серию образовательных проектов по enterprise AI engineering. LLM Gateway — первый "кирпичик". Идея: корпорации хотят использовать LLM, но не могут пускать трафик напрямую к Anthropic/OpenAI без контроля. Нужен прокси внутри корпоративного контура с логированием, санитайзингом и возможностью подключить любую LLM.

### Что смотрел из аналогов

Изучил весь рынок LLM gateway: LiteLLM (40K stars, Python), Portkey (TypeScript, enterprise), Helicone (Rust, observability), Kong AI Gateway (Lua, enterprise plugins), Bifrost (Go, 11µs overhead), TensorZero (Rust, ML-driven routing).

**Главный инсайт из исследования:** все существующие решения либо слишком developer-tool-oriented (LiteLLM), либо слишком enterprise-locked (Kong). Нет решения, которое было бы одновременно простым для понимания И правильно спроектированным для корпоративного контекста.

**Неожиданное:** в марте 2026 LiteLLM пережил supply chain атаку через PyPI — compromised пакеты крали API ключи. Это укрепило выбор в пользу проекта с минимальными зависимостями.

### Ключевые решения сегодня

**Python vs Go/Rust:** выбрал Python несмотря на меньшую производительность. Причина: экосистема, читаемость для обучения, богатые NLP библиотеки для будущих sanitizer'ов. Для образовательного проекта важнее понятность кода, чем 11µs latency.

**ASGI Middleware vs Event Bus:** рассматривал два варианта. Event bus дал бы лучшую latency для логирования, но для audit trail нужна гарантия записи ДО отдачи ответа клиенту. ASGI middleware даёт это из коробки. Компромисс: чуть медленнее, зато compliance-корректно по умолчанию.

**Важное уточнение по Auth:** изначально думал о встроенной auth логике (API keys в конфиге). Но это неправильно для corporate tool — у каждой компании своя система (LDAP, Okta, JWT). Сделал `BaseAuthMiddleware` абстрактным. Gateway = каркас, логика = на пользователе. Это паттерн всех трёх точек расширения: Auth, Sanitizer, Adapter.

**Про Claude Code интеграцию:** обнаружил, что Claude Code поддерживает `ANTHROPIC_BASE_URL` для перенаправления трафика через корпоративный gateway. Это меняет приоритеты — в будущем нужна поддержка Anthropic Messages API (`/v1/messages`), не только OpenAI-compatible. Для v1 оставляем `/v1/chat/completions`, Claude proxy — в бэклог.

### Что было неочевидно

OpenAI API стал де-факто стандартом не только для OpenAI — практически все новые LLM провайдеры (DeepSeek, локальные через vLLM/Ollama) реализуют OpenAI-compatible endpoint. Это значит, что `OpenAICompatibleAdapter` через конфиг покроет ~90% реальных use cases без единой строки Python кода от пользователя.

---

## 2026-06-06 — Баг с порядком middleware и приватность данных

### Что случилось

В процессе реализации столкнулся с нетривиальным конфликтом двух требований:

1. **Audit должен быть ПОСЛЕ sanitize** — чтобы Audit никогда не видел сырые данные (принцип privacy-by-design)
2. **Blocked запросы должны аудироваться** — design doc явно требует запись с `status=blocked`

С `BaseHTTPMiddleware` это конфликт. Если порядок Auth → Sanitize → Audit → Route, то когда Sanitize блокирует запрос и возвращает 400 без вызова `call_next`, Audit никогда не запускается.

### Первая "починка" была неправильной

Интеграционные тесты обнаружили, что `test_blocked_request_writes_audit_with_status_blocked` падает. Автоматическое "исправление": поменять порядок на Auth → Audit → Sanitize → Route. Тест прошёл. Но это создало другую проблему: Audit теперь оборачивает Sanitize и видит запрос ДО санитайзинга.

Это было замечено при ревью: если в v3 добавить full request logging в AuditMiddleware, приватные данные попадут в лог несанитайзированными.

### Правильное решение

Изменил архитектуру взаимодействия Sanitize и Route:

- **SanitizeMiddleware при блокировке** — не возвращает 400 напрямую. Вместо этого: сохраняет ошибку в `request.state.blocked_error`, устанавливает `request.state.audit_status = "blocked"`, и **всё равно вызывает `call_next`**.
- **Route handler** — первым делом проверяет `request.state.blocked_error` и возвращает 400 если установлен.
- **Порядок остаётся** Auth → Sanitize → Audit → Route.

Результат:
- Audit всегда запускается для аутентифицированных запросов (в том числе blocked) ✓
- Audit видит запрос только ПОСЛЕ санитайзинга ✓
- 401 не аудируются (Auth short-circuits перед Sanitize и Audit) ✓

### Урок

`BaseHTTPMiddleware` — это вложенные обёртки. Middleware, добавленный последним через `add_middleware()`, становится самым внешним. Ранний `return` без `call_next` не просто "отвечает клиенту" — он предотвращает запуск всех более внутренних middleware. Это важно учитывать при проектировании стека: порядок не только логический ("что первым обрабатывает запрос"), но и определяет, какие middleware вообще запустятся в разных сценариях.

---

## 2026-06-07 — v2.2: Audit Backends

### Что сделали

Заменили захардкоженный `StdoutAuditBackend()` в `create_app()` на конфигурируемый audit backend через discriminated union в `AuditConfig`.

Три варианта:
- `type: stdout` — JSON lines в stdout (был раньше, теперь через конфиг)
- `type: file` — JSON lines в файл (новый `FileAuditBackend`)
- `type: plugin` — кастомный класс через `module:` (тот же паттерн, что у адаптеров и санитайзеров)

### Ключевое решение: discriminated union vs module/config

Рассматривали два варианта для `AuditConfig`:
1. **module/config** — единый `{module: "...", config: {...}}` как у sanitizers. Гибко, но теряем Pydantic валидацию параметров `file` backend при старте.
2. **Discriminated union** — `type: stdout | file | plugin`, каждый тип — отдельная Pydantic модель.

Выбрали discriminated union. Причина: `FileAuditConfig` требует обязательный `path` — с discriminated union Pydantic валидирует это при старте приложения, до первого запроса. `type: file` без `path` → `ValidationError` немедленно, не в runtime.

**Breaking change:** `audit: {backend: "stdout"}` → `audit: {type: stdout}`. Документировано.

### FileAuditBackend — намеренные ограничения

Синхронные `write` + `flush` без `close()`. Процесс-ориентированный lifecycle: OS закрывает FD при завершении. Явные lifecycle hooks (`startup`/`shutdown`) — вне scope v2.2. Нет ротации, нет буферизации.

Трейдоф осознан: это reference implementation для обучения, не production-hardened решение.

---

## 2026-06-10 — Streaming: дизайн и трейдофы

### Почему стриминг важен

Большинство реальных клиентов — OpenAI SDK, LangChain, агентские фреймворки — по умолчанию отправляют `"stream": true`. Без поддержки они либо падают с ошибкой, либо ждут полный ответ. Для длинных генераций это неприемлемо: клиент таймаутится до получения хоть какого-то ответа.

### Главный вопрос: что делать с output sanitizer

Сразу стало ясно, что chunk-by-chunk санитайзация невозможна. NER-модель (Presidio) строит entities на полном контексте — "Иван" и "Иванов" из разных чанков не соберутся в `PERSON`. Это не компромисс, это архитектурная невозможность.

Рассматривал три варианта:

**Вариант A (pseudo-streaming):** gateway буферизует весь ответ LLM, санитайзит, потом отдаёт клиенту как SSE. Клиент не таймаутится, но настоящего TTFF нет — клиент всё равно ждёт.

**Вариант B (upstream streaming + buffer):** стримим от LLM к себе, буферизуем, санитайзим, потом отдаём. Те же проблемы что у A — TTFF = полное время ответа.

**Вариант C (true end-to-end):** стримим от LLM сразу к клиенту, output sanitizer убираем из критического пути. Для audit body logging — запускаем постфактум на полном тексте в `finally`.

Выбрал C. Ключевой довод: output sanitizer на ответе имеет смысл только если **gateway знает больше, чем клиент** — например, RAG, где gateway подмешивает данные других пользователей. В нашем сценарии клиент уже знает всё, что знает gateway. Санитайзировать ответ от клиента для клиента же — бессмысленно.

Плюс, выяснилось, что output sanitizer в `complete()` был wired, но на самом деле никогда не вызывался (result.content, а не sanitized version шёл в response). Это обнаружили при ревью. В `complete_stream()` сделали явно: output sanitizer вызывается только для audit body logging.

### Audit при стриминге: try/finally

Стриминг делает audit трудным: клиент может отключиться в любой момент. Python даёт `GeneratorExit` (BaseException, не Exception) когда генератор закрывается через `aclose()`. `try/except Exception` его не поймает.

Решение: `try/except/finally` вокруг streaming loop. `finally` выполняется всегда — при успехе, при исключении, и при `GeneratorExit`. В `finally` пишем audit безусловно.

Для разграничения "нормально завершился" vs "клиент отключился" — флаг `stream_complete = False`, который ставится в `True` только после выхода из `async for`. В `finally`: `if not stream_complete and status == "success": status = "cancelled"`.

### Usage токены в стриминге

OpenAI отдаёт usage не в последнем контентном чанке, а в отдельном чанке **после** `[DONE]`. Это значит, что наивная реализация с `break` при `[DONE]` потеряет usage.

Решение: флаг `stream_done = False`. При `[DONE]` ставим `stream_done = True`, но продолжаем итерировать. Контент после `[DONE]` не yielded (`if stream_done: continue`), но usage chunk обрабатывается. Это гарантирует, что `usage_out` будет заполнен даже если провайдер отдаёт usage после `[DONE]`.

### Ошибки как SSE events

Проблема: после первого байта ответа HTTP статус уже отправлен (200). Нельзя вернуть 400 или 500. Решение — сериализовать ошибки как SSE события с `error` полем:

```
data: {"error": {"type": "gateway_error", "code": "upstream_timeout", "message": "..."}}

data: [DONE]
```

`data: [DONE]` гарантирован через `finally` в `_sse_stream()` — даже после error events. OpenAI клиенты ожидают `[DONE]` для завершения стрима.

### Что стало неожиданным

В начале думал, что стриминг — это просто "передай чанки клиенту". Оказалось, основная сложность — правильная обработка граничных случаев: usage-after-DONE, client disconnect в finally, error serialization без смены HTTP статуса, guards против `choices[0] = null` в streaming response. Сама передача чанков — тривиальна.
