# Audit Backends — Design Document

**Date:** 2026-06-07
**Status:** Approved
**Author:** Alexander Melnik
**Version:** v2.2

---

## Context and Purpose

The v2.1 gateway contains only one audit backend — `StdoutAuditBackend` (JSON lines to stdout). It is hardcoded in `create_app()` and is not configurable via `gateway.yaml`.

v2.2 takes three steps:

1. Migrates `AuditConfig` to a discriminated union following the pattern used for adapters — Pydantic validates the parameters of each backend at application startup.
2. Adds `FileAuditBackend` — writes JSON lines to a file, creates the directory automatically.
3. Wires the audit backend from config in `create_app()` — removes the hardcoded `StdoutAuditBackend()`.

---

## Scope

**In scope:**
- Discriminated union `AuditConfig`: `stdout` / `file` / `plugin`
- `FileAuditBackend` without rotation
- Wiring audit backend from config in `create_app()`
- Update `gateway.yaml.example`
- 4 unit tests for `FileAuditBackend` + 2 unit tests for config validation
- 1 integration test
- Update `docs/2026-06-05-llm-gateway-design.md` and `docs/DEVLOG.md`

**Explicitly out of scope:**
- PostgresBackend — v2.3
- File rotation
- Buffering / batching of writes
- Asynchronous file operations (`aiofiles`)

---

## Architecture

### Config Schema

```python
# gateway/config.py

class StdoutAuditConfig(BaseModel):
    type: Literal["stdout"] = "stdout"

class FileAuditConfig(BaseModel):
    type: Literal["file"]
    path: str                              # required — ValidationError if missing

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
    audit: AuditConfig = StdoutAuditConfig()   # default — backward compatibility
```

**Breaking change:** the old format `audit: {backend: "stdout"}` no longer works. New format: `audit: {type: "stdout"}`. `gateway.yaml.example` is updated.

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

Synchronous `write` + `flush` — justified for audit logging (low volume, immediate disk write required). No external dependencies.

Record format — JSON line, identical to `StdoutAuditBackend`. Single file, append mode.

`FileAuditBackend` does not implement explicit file closing (`close()`). In the context of a gateway — the process lives until termination, and the OS closes file descriptors on exit. An explicit lifecycle (startup/shutdown hooks) is out of scope for v2.2.

### Wiring in create_app()

```python
# gateway/app.py — replace the hardcoded StdoutAuditBackend()

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

The same pattern with the same error handling already used for adapters and sanitizers.

### gateway.yaml (new format)

```yaml
audit:
  type: stdout   # stdout | file | plugin

# Or for file backend:
# audit:
#   type: file
#   path: "/var/log/gateway/audit.jsonl"

# Or for a custom backend:
# audit:
#   type: plugin
#   module: "my_backends.corp_audit.CorpAuditBackend"
#   config:
#     endpoint: "https://audit.corp.internal"
#     token_env: AUDIT_TOKEN
```

---

## Error Handling

| Situation | Behavior |
|----------|-----------|
| `type: file` without `path` | `ValidationError` at `load_config()` — before server starts |
| `path` in a non-existent directory | `mkdir(parents=True)` in `__init__` — directory is created automatically |
| File write error | `IOError` is propagated — AuditMiddleware will return 500 (v1 behavior) |
| `type: plugin` — invalid `module` | `ValueError: Cannot load audit backend '...'` — same as sanitizers |
| Old `backend: "stdout"` in YAML | `ValidationError` — explicit breaking change, documented in DEVLOG |

---

## Testing

### Unit — `tests/unit/test_file_audit_backend.py`

| Test | What it verifies |
|------|---------------|
| `test_write_creates_file` | backend creates the file if it does not exist |
| `test_write_appends_json_line` | each `write()` call appends a valid JSON line |
| `test_write_multiple_records` | two calls → two lines in the file |
| `test_init_creates_parent_dirs` | directory is created if it does not exist |

### Unit — `tests/unit/test_config.py` (extend)

| Test | What it verifies |
|------|---------------|
| `test_file_audit_config_requires_path` | `type: file` without `path` → `ValidationError` |
| `test_stdout_audit_config_default` | empty `audit: {type: stdout}` → `StdoutAuditConfig` |

### Integration — `tests/integration/test_middleware_stack.py`

`test_audit_writes_to_file` — request through the full stack with `FileAuditBackend(tmp_path)`, verify that a valid JSON line with `status: "success"` appeared in the file.

---

## Files

| Action | File |
|----------|------|
| Create | `gateway/audit/file_backend.py` |
| Modify | `gateway/config.py` — AuditConfig discriminated union |
| Modify | `gateway/app.py` — wire audit backend from config |
| Modify | `gateway.yaml.example` — new format + examples |
| Create | `tests/unit/test_file_audit_backend.py` |
| Modify | `tests/unit/test_config.py` — 2 new tests |
| Modify | `tests/integration/test_middleware_stack.py` — 1 test |
| Modify | `docs/2026-06-05-llm-gateway-design.md` |
| Modify | `docs/DEVLOG.md` |
