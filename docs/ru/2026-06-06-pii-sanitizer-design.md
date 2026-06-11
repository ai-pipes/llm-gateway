# PiiRegexSanitizer — Design Document

**Date:** 2026-06-06  
**Status:** Approved  
**Author:** Alexander Melnik  
**Version:** v2.1

---

## Контекст и цель

v1 gateway содержит полностью рабочий `SanitizerChain`, но цепочки всегда пустые — `create_app()` их не заполняет из конфига. v2.1 делает два шага:

1. Подключает sanitizer'ы из `gateway.yaml` (wire из конфига).
2. Добавляет первый реальный sanitizer — `PiiRegexSanitizer`, который детектирует и обрабатывает email, телефон и номера карт через regex.

---

## Скоуп

**В scope:**
- `PiiRegexSanitizer` с режимами `replace` и `block`
- PII-типы: email, phone (универсальный паттерн), card (regex, без Luhn)
- Wiring sanitizer'ов из `gateway.yaml` в `create_app()`
- 8 unit тестов + 1 integration тест

**Явно вне scope:**
- NLP-санитайзеры (spaCy, Presidio) — v3
- Output sanitization (output chain остаётся пустой)
- Новые PII-типы
- Luhn-валидация для карт

---

## Архитектура

### PiiRegexSanitizer

```python
# gateway/sanitizers/pii_regex.py
class PiiRegexSanitizer(BaseSanitizer):
    def __init__(self, mode: str = "replace"):
        # mode: "replace" | "block"
```

Три паттерна:

| Тип | Regex | Placeholder |
|-----|-------|-------------|
| Email | `[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}` | `[EMAIL]` |
| Phone | `\+?[\d\s\-\(\)]{10,15}` | `[PHONE]` | ¹ |
| Card | `\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}` | `[CARD]` |

Порядок применения: Email → Phone → Card. Фиксированный порядок — детерминированный результат.

¹ Phone regex намеренно широкий (минимальный универсальный паттерн). Возможны false positives на длинные числа с пробелами. Это осознанный трейдоф: локаль-независимость важнее точности в v2. Для точного детектирования — NLP-санитайзер в v3.

**Режим `replace`:**
- Все вхождения каждого типа заменяются placeholder'ом
- `blocked=False`
- `actions` содержит метку для каждого найденного типа: `["replaced:EMAIL", "replaced:PHONE"]` — дедуплицировано по типу, не по количеству вхождений. Два email в тексте → одна запись `"replaced:EMAIL"` в actions.

**Режим `block`:**
- При первом найденном PII любого типа: `blocked=True`, `block_reason="pii_detected:EMAIL"` (первый найденный тип)
- `actions=["blocked:EMAIL"]`
- Текст возвращается без изменений (LLM не вызывается)

**Текст без PII:** `SanitizeResult(text=original, actions=[], blocked=False)` — в обоих режимах.

### Wiring из конфига

Изменение в `gateway/app.py` — `create_app()`:

```python
# Вместо: input_chain = SanitizerChain([])
input_sanitizers = []
for s_conf in config.sanitizers.input:
    mod_path, cls_name = s_conf.module.rsplit(".", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    input_sanitizers.append(cls(**s_conf.config))
input_chain = SanitizerChain(input_sanitizers)

output_chain = SanitizerChain([])  # output — v3
```

`SanitizerItemConfig` в `config.py` уже имеет нужные поля (`module: str`, `config: dict`). Изменений в схеме конфига нет.

### Конфиг (gateway.yaml)

```yaml
sanitizers:
  input:
    - module: "gateway.sanitizers.pii_regex.PiiRegexSanitizer"
      config:
        mode: replace   # или block
  output: []
```

---

## Обработка ошибок

Неверное значение `mode` (не `replace` и не `block`) → `ValueError` при инициализации, до старта сервера. Быстрый fail — лучше чем молчаливое игнорирование в рантайме.

Ошибка импорта sanitizer'а (неверный `module`) → `ImportError` при старте. Аналогично поведению адаптеров в `create_app()`.

---

## Тестирование

### Unit тесты — `tests/unit/test_pii_sanitizer.py`

| Тест | Что проверяет |
|------|---------------|
| `test_replace_email` | email заменяется на `[EMAIL]`, остальной текст сохраняется |
| `test_replace_phone` | телефон заменяется на `[PHONE]` |
| `test_replace_card` | номер карты заменяется на `[CARD]` |
| `test_replace_multiple_types` | несколько типов PII в одном тексте — все заменяются |
| `test_replace_actions_contain_labels` | `actions == ["replaced:EMAIL", "replaced:CARD"]` |
| `test_no_pii_passthrough` | текст без PII проходит неизменным, `actions == []` |
| `test_block_mode_returns_blocked` | `mode=block`, PII найден → `blocked=True` |
| `test_block_mode_no_pii_passes` | `mode=block`, PII нет → проходит нормально |

### Integration тест — добавить в `tests/integration/test_middleware_stack.py`

`test_pii_replaced_in_request` — запрос с email в тексте сообщения, `PiiRegexSanitizer(mode="replace")` в input chain, ответ приходит успешно, аудит содержит `input_actions: ["replaced:EMAIL"]`.

---

## Файлы

| Действие | Файл |
|----------|------|
| Создать | `gateway/sanitizers/pii_regex.py` |
| Изменить | `gateway/app.py` — wire sanitizers из конфига |
| Изменить | `gateway.yaml.example` — раскомментировать пример |
| Создать | `tests/unit/test_pii_sanitizer.py` |
| Изменить | `tests/integration/test_middleware_stack.py` — 1 тест |
