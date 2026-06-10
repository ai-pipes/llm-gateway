# DEVLOG — LLM Gateway

Хронологический журнал разработки. Здесь — ход мыслей, тупики, инсайты. Архитектурные решения с обоснованием — в `adr/`.

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
