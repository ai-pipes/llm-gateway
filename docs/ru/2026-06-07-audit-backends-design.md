# Audit Backends — Design Document

**Date:** 2026-06-07
**Status:** Approved
**Author:** Alexander Melnik
**Version:** v2.2

---

## Контекст и цель

v2.1 gateway содержит только один audit backend — `StdoutAuditBackend` (JSON lines в stdout). Он захардкожен в `create_app()` и не настраивается через `gateway.yaml`.

v2.2 делает три шага:

1. Переводит `AuditConfig` на discriminated union по образцу адаптеров — Pydantic валидирует параметры каждого backend при старте приложения.
2. Добавляет `FileAuditBackend` — пишет JSON lines в файл, создаёт директорию автоматически.
3. Подключает audit backend из конфига в `create_app()` — убирает хардкод `StdoutAuditBackend()`.

---

## Скоуп

**В scope:**
- Discriminated union `AuditConfig`: `stdout` / `file` / `plugin`
- `FileAuditBackend` без ротации
- Wiring audit backend из конфига в `create_app()`
- Обновление `gateway.yaml.example`
- 4 unit теста для `FileAuditBackend` + 2 unit теста для config validation
- 1 integration тест
- Обновление `docs/2026-06-05-llm-gateway-design.md` и `docs/DEVLOG.md`

**Явно вне scope:**
- PostgresBackend — v2.3
- Ротация файлов
- Буферизация / батчинг записей
- Асинхронные файловые операции (`aiofiles`)

---

## Архитектура

### Config schema

```python
# gateway/config.py

class StdoutAuditConfig(BaseModel):
    type: Literal["stdout"] = "stdout"

class FileAuditConfig(BaseModel):
    type: Literal["file"]
    path: str                              # обязательно — ValidationError если отсутствует

class PluginAuditConfig(BaseModel):
    type: Literal["plugin"]
    module: str
    config: dict = Field(default_factory=dict)

AuditConfig = Annotated[
    StdoutAuditConfig | FileAuditConfig | PluginAuditConfig,
    Field(discriminator="type")
]

class Config(BaseModel):
    ...
    audit: AuditConfig = StdoutAuditConfig()   # default — обратная совместимость
```

**Breaking change:** старый формат `audit: {backend: "stdout"}` больше не работает. Новый формат: `audit: {type: "stdout"}`. `gateway.yaml.example` обновляется.

### FileAuditBackend

```python
# gateway/audit/file_backend.py

import json
from pathlib import Path
from gateway.audit.base import BaseAuditBackend
from gateway.audit.record import AuditRecord


class FileAuditBackend(BaseAuditBackend):
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8")

    async def write(self, record: AuditRecord) -> None:
        line = json.dumps(record.__dict__, default=str)
        self._file.write(line + "\n")
        self._file.flush()
```

Синхронные `write` + `flush` — оправданно для audit logging (малый объём, нужна немедленная запись на диск). Без внешних зависимостей.

Формат записи — JSON line, идентичен `StdoutAuditBackend`. Один файл, append mode.

`FileAuditBackend` не реализует явного закрытия файла (`close()`). В контексте gateway — процесс живёт до завершения, OS закрывает файловые дескрипторы при выходе. Явный lifecycle (startup/shutdown hooks) — вне scope v2.2.

### Wiring в create_app()

```python
# gateway/app.py — заменить захардкоженный StdoutAuditBackend()

match config.audit.type:
    case "stdout":
        audit_backend = StdoutAuditBackend()
    case "file":
        audit_backend = FileAuditBackend(path=config.audit.path)
    case "plugin":
        try:
            mod_path, cls_name = config.audit.module.rsplit(".", 1)
            mod = importlib.import_module(mod_path)
            cls = getattr(mod, cls_name)
            audit_backend = cls(**config.audit.config)
        except (ImportError, AttributeError, ValueError, TypeError) as e:
            raise ValueError(f"Cannot load audit backend '{config.audit.module}': {e}") from e
```

Тот же паттерн с той же обработкой ошибок, что уже используется для адаптеров и sanitizers.

### gateway.yaml (новый формат)

```yaml
audit:
  type: stdout   # stdout | file | plugin

# Или для file backend:
# audit:
#   type: file
#   path: "/var/log/gateway/audit.jsonl"

# Или для кастомного backend:
# audit:
#   type: plugin
#   module: "my_backends.corp_audit.CorpAuditBackend"
#   config:
#     endpoint: "https://audit.corp.internal"
#     token_env: AUDIT_TOKEN
```

---

## Обработка ошибок

| Ситуация | Поведение |
|----------|-----------|
| `type: file` без `path` | `ValidationError` при `load_config()` — до старта сервера |
| `path` в несуществующей директории | `mkdir(parents=True)` в `__init__` — директория создаётся автоматически |
| Ошибка записи в файл | `IOError` пробрасывается — AuditMiddleware вернёт 500 (поведение v1) |
| `type: plugin` — неверный `module` | `ValueError: Cannot load audit backend '...'` — аналогично sanitizers |
| Старый `backend: "stdout"` в YAML | `ValidationError` — явный breaking change, описан в DEVLOG |

---

## Тестирование

### Unit — `tests/unit/test_file_audit_backend.py`

| Тест | Что проверяет |
|------|---------------|
| `test_write_creates_file` | backend создаёт файл если не существует |
| `test_write_appends_json_line` | каждый вызов `write()` добавляет валидный JSON line |
| `test_write_multiple_records` | два вызова → две строки в файле |
| `test_init_creates_parent_dirs` | директория создаётся если нет |

### Unit — `tests/unit/test_config.py` (расширить)

| Тест | Что проверяет |
|------|---------------|
| `test_file_audit_config_requires_path` | `type: file` без `path` → `ValidationError` |
| `test_stdout_audit_config_default` | пустой `audit: {type: stdout}` → `StdoutAuditConfig` |

### Integration — `tests/integration/test_middleware_stack.py`

`test_audit_writes_to_file` — запрос через полный стек с `FileAuditBackend(tmp_path)`, проверяем что в файле появилась валидная JSON line с `status: "success"`.

---

## Файлы

| Действие | Файл |
|----------|------|
| Создать | `gateway/audit/file_backend.py` |
| Изменить | `gateway/config.py` — AuditConfig discriminated union |
| Изменить | `gateway/app.py` — wire audit backend из конфига |
| Изменить | `gateway.yaml.example` — новый формат + примеры |
| Создать | `tests/unit/test_file_audit_backend.py` |
| Изменить | `tests/unit/test_config.py` — 2 новых теста |
| Изменить | `tests/integration/test_middleware_stack.py` — 1 тест |
| Изменить | `docs/2026-06-05-llm-gateway-design.md` |
| Изменить | `docs/DEVLOG.md` |
