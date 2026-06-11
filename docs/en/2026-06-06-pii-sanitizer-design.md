# PiiRegexSanitizer — Design Document

**Date:** 2026-06-06  
**Status:** Approved  
**Author:** Alexander Melnik  
**Version:** v2.1

---

## Context and Purpose

The v1 gateway contains a fully functional `SanitizerChain`, but the chains are always empty — `create_app()` does not populate them from the config. v2.1 takes two steps:

1. Wires sanitizers from `gateway.yaml` (wire from config).
2. Adds the first real sanitizer — `PiiRegexSanitizer`, which detects and handles email addresses, phone numbers, and card numbers via regex.

---

## Scope

**In scope:**
- `PiiRegexSanitizer` with `replace` and `block` modes
- PII types: email, phone (universal pattern), card (regex, no Luhn)
- Wiring sanitizers from `gateway.yaml` into `create_app()`
- 8 unit tests + 1 integration test

**Explicitly out of scope:**
- NLP sanitizers (spaCy, Presidio) — v3
- Output sanitization (output chain stays empty)
- New PII types
- Luhn validation for cards

---

## Architecture

### PiiRegexSanitizer

```python
# gateway/sanitizers/pii_regex.py
class PiiRegexSanitizer(BaseSanitizer):
    def __init__(self, mode: str = "replace"):
        # mode: "replace" | "block"
```

Three patterns:

| Type | Regex | Placeholder |
|-----|-------|-------------|
| Email | `[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}` | `[EMAIL]` |
| Phone | `\+?[\d\s\-\(\)]{10,15}` | `[PHONE]` | ¹ |
| Card | `\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}` | `[CARD]` |

Application order: Email → Phone → Card. Fixed order — deterministic result.

¹ The Phone regex is intentionally broad (minimal universal pattern). False positives are possible on long numbers with spaces. This is a deliberate trade-off: locale-independence matters more than precision in v2. For accurate detection — use an NLP sanitizer in v3.

**`replace` mode:**
- All occurrences of each type are replaced with the placeholder
- `blocked=False`
- `actions` contains a label for each found type: `["replaced:EMAIL", "replaced:PHONE"]` — deduplicated by type, not by number of occurrences. Two emails in the text → one `"replaced:EMAIL"` entry in actions.

**`block` mode:**
- On the first detected PII of any type: `blocked=True`, `block_reason="pii_detected:EMAIL"` (the first found type)
- `actions=["blocked:EMAIL"]`
- Text is returned unchanged (LLM is not called)

**Text without PII:** `SanitizeResult(text=original, actions=[], blocked=False)` — in both modes.

### Wiring from Config

Change in `gateway/app.py` — `create_app()`:

```python
# Instead of: input_chain = SanitizerChain([])
input_sanitizers = []
for s_conf in config.sanitizers.input:
    mod_path, cls_name = s_conf.module.rsplit(".", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    input_sanitizers.append(cls(**s_conf.config))
input_chain = SanitizerChain(input_sanitizers)

output_chain = SanitizerChain([])  # output — v3
```

`SanitizerItemConfig` in `config.py` already has the required fields (`module: str`, `config: dict`). No changes to the config schema.

### Config (gateway.yaml)

```yaml
sanitizers:
  input:
    - module: "gateway.sanitizers.pii_regex.PiiRegexSanitizer"
      config:
        mode: replace   # or block
  output: []
```

---

## Error Handling

An invalid `mode` value (neither `replace` nor `block`) → `ValueError` at initialization, before the server starts. Fail fast — better than silently ignoring at runtime.

A sanitizer import error (invalid `module`) → `ImportError` at startup. Behaves the same as adapters in `create_app()`.

---

## Testing

### Unit Tests — `tests/unit/test_pii_sanitizer.py`

| Test | What it verifies |
|------|---------------|
| `test_replace_email` | email is replaced with `[EMAIL]`, the rest of the text is preserved |
| `test_replace_phone` | phone number is replaced with `[PHONE]` |
| `test_replace_card` | card number is replaced with `[CARD]` |
| `test_replace_multiple_types` | multiple PII types in one text — all are replaced |
| `test_replace_actions_contain_labels` | `actions == ["replaced:EMAIL", "replaced:CARD"]` |
| `test_no_pii_passthrough` | text without PII passes unchanged, `actions == []` |
| `test_block_mode_returns_blocked` | `mode=block`, PII found → `blocked=True` |
| `test_block_mode_no_pii_passes` | `mode=block`, no PII → passes normally |

### Integration Test — add to `tests/integration/test_middleware_stack.py`

`test_pii_replaced_in_request` — request with an email in the message text, `PiiRegexSanitizer(mode="replace")` in the input chain, response arrives successfully, audit contains `input_actions: ["replaced:EMAIL"]`.

---

## Files

| Action | File |
|----------|------|
| Create | `gateway/sanitizers/pii_regex.py` |
| Modify | `gateway/app.py` — wire sanitizers from config |
| Modify | `gateway.yaml.example` — uncomment example |
| Create | `tests/unit/test_pii_sanitizer.py` |
| Modify | `tests/integration/test_middleware_stack.py` — 1 test |
