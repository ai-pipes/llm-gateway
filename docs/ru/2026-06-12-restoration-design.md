# PII Restoration — Дизайн-документ

**Date:** 2026-06-12
**Status:** Implemented
**Author:** Alexander Melnik
**Version:** v3.2 / v3.3

---

## Контекст и цель

Input sanitizer gateway маскирует PII до того, как данные попадают в LLM — email, телефоны, номера карт становятся токенами вида `[EMAIL_ADDRESS]`. Проблема: ответ LLM тоже содержит эти токены. Клиент отправляет реальный email и получает placeholder обратно.

Цель: восстановить оригинальные значения в ответе LLM, чтобы клиент никогда не видел placeholder-ов, без отправки реальных данных в LLM.

v3.2 покрывает не-стриминговый путь (`complete()`). v3.3 добавляет стриминг через look-ahead буфер в `StreamingRestorer`.

---

## Ключевое решение: RestorationContext

Центральный вопрос — где хранить маппинг `placeholder → original`, созданный во время санитайзации, и как передать его на этап восстановления после получения ответа.

### Два варианта

**Вариант A: Маппинг на объекте запроса**
Хранить маппинг в `request.state` (FastAPI) или в общем словаре. Просто в реализации, но: маппинг привязан к жизненному циклу HTTP-запроса, сложнее тестировать изолированно, перемешиваются слои HTTP и приложения.

**Вариант B: Отдельный объект `RestorationContext` (выбран)**
Создавать `RestorationContext` в начале `ChatService.complete()`, передавать его в цепочку санитайзеров, собирать маппинг там, восстанавливать ответ в конце.

Выбран B. Контекст имеет чёткий жизненный цикл (один на вызов `complete()`), тривиально тестируется и не смешивает HTTP и application слои.

---

## Как это работает

```
complete(raw_messages)
    │
    ├─ context = RestorationContext()          # пустой маппинг
    │
    ├─ _sanitize_input(messages, context)
    │       │
    │       └─ для каждого message content:
    │               sanitizer.sanitize(text, context)
    │                   → заменяет PII уникальными placeholder-ами
    │                   → регистрирует каждый: context.register(original, entity_type)
    │
    ├─ if context.has_replacements():
    │       inject system instruction в messages
    │       "preserve placeholders — do not modify them"
    │
    ├─ adapter.chat(sanitized_messages)        # LLM видит только placeholder-ы
    │       → response.content может содержать placeholder-ы
    │
    ├─ sanitized_content = response.content    # сохраняем для audit ДО восстановления
    │
    ├─ if context.has_replacements():
    │       response = replace(response, content=context.restore(response.content))
    │
    └─ audit.write(completion=sanitized_content)   # audit никогда не логирует сырой PII
```

---

## RestorationContext

```python
class RestorationContext:
    def register(self, original: str, entity_type: str) -> str:
        # дедупликация: одинаковый original → тот же placeholder
        if original in self._reverse:
            return self._reverse[original]
        placeholder = f"[{entity_type.upper()}_{secrets.token_hex(4)}]"
        self._map[placeholder] = original
        self._reverse[original] = placeholder
        return placeholder

    def restore(self, text: str) -> str:
        for placeholder, original in self._map.items():
            text = text.replace(placeholder, original)
        return text
```

**Дедупликация:** если один и тот же email встречается дважды в сообщении, он получает один placeholder — LLM получает чистый, нередундантный контекст, а маппинг остаётся компактным.

**Уникальность:** `secrets.token_hex(4)` даёт 8 hex-символов (4 байта = 2³² вариантов). Подобрать невозможно. Два разных значения всегда получают разные placeholder-ы даже при одинаковом типе сущности.

**Формат placeholder-а:** `[EMAIL_ADDRESS_3f2a1b0c]`. Тип сущности в верхнем регистре, hex-суффикс в нижнем, квадратные скобки. Названия типов сущностей Presidio используются как есть (`EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD`), лейблы regex-санитайзера приводятся к верхнему регистру (`EMAIL`, `PHONE`, `CARD`).

---

## Инъекция системной инструкции

Когда зарегистрирована хотя бы одна замена, системная инструкция вставляется в начало messages (или добавляется к существующему system message):

> IMPORTANT: Some values in this message have been replaced with opaque placeholder tokens matching the pattern [TYPENAME_HEXCHARS] (e.g. [EMAIL_ADDRESS_3f2a1b0c], [PHONE_NUMBER_9d4e7a12]). These are NOT real values — do not invent, guess, or reconstruct the originals. Preserve every such token exactly as written — do not modify, translate, paraphrase, or remove them.

**Ключевое ограничение:** примеры в инструкции должны выглядеть явно ненастоящими. При тестировании LLM (gpt-4o-mini) дословно скопировал реально выглядящий placeholder из примера в свой ответ — использовал его как generic-шаблон вместо зарегистрированного. Примеры вида `[EMAIL_ADDRESS_3f2a1b0c]` с пометкой "These are NOT real values" решили проблему.

**Логика вставки:**
```python
def _inject_system_instruction(messages, instruction):
    for i, msg in enumerate(messages):
        if msg.get("role") == "system" and isinstance(msg.get("content"), str):
            # добавляем к существующему system message
            updated = {**msg, "content": msg["content"] + "\n\n" + instruction}
            return messages[:i] + [updated] + messages[i + 1:]
    # нет system message — вставляем новый в начало
    return [{"role": "system", "content": instruction}] + messages
```

---

## Безопасность аудита

Запись аудита не должна содержать сырой PII — иначе санитайзация теряет смысл.

Последовательность в `complete()`:
```python
sanitized_content = response.content          # 1. сохраняем версию с placeholder-ами
if context.has_replacements():
    response = replace(response, content=context.restore(response.content))
                                              # 2. восстанавливаем для клиента
await audit.write(..., completion=sanitized_content)
                                              # 3. аудит получает версию с placeholder-ами
```

Порядок критичен. `sanitized_content` захватывается до вызова `context.restore()`. Аудит всегда получает версию с placeholder-ами, даже если клиент получает восстановленную.

---

## Изменения в санитайзерах

### `BaseSanitizer` и `SanitizerChain`

Добавлен необязательный параметр `context: RestorationContext | None = None` в `sanitize()` и `SanitizerChain.run()`. По умолчанию `None` — обратная совместимость. Существующие санитайзеры, игнорирующие context, продолжают работать без изменений.

Импорт через `TYPE_CHECKING` для избежания циклической зависимости:
```python
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from gateway.domain.sanitizers.restoration import RestorationContext
```

### `PiiRegexSanitizer`

Использует `re.sub` с callback вместо фиксированной строки замены:

```python
def replacer(m, _label=label):   # default arg для избежания late-binding closure bug
    if context:
        return context.register(m.group(), _label)
    return f"[{_label}]"
new_text = pattern.sub(replacer, text)
```

Аргумент по умолчанию `_label=label` необходим: без него все замыкания в цикле захватили бы одно и то же финальное значение `label` (Python late-binding).

### `PresidioSanitizer`

При наличии `context` обходит `AnonymizerEngine.anonymize()` и выполняет ручную замену спанов справа налево. Порядок справа налево сохраняет более ранние индексы: когда спан заменяется более длинным placeholder-ом, последующие спаны с меньшими позициями не затрагиваются.

**Обнаруженная проблема:** Presidio иногда возвращает перекрывающиеся спаны. Например, `sarah@techcorp.io` определяется одновременно как `EMAIL_ADDRESS` (score 1.0) и как `URL` для `techcorp.io` (score 0.5). Замена справа налево сначала обрабатывает `URL` (больший start-индекс), вставляет `[URL_xxxxxxxx]` в строку, потом пытается заменить `EMAIL_ADDRESS` по уже сдвинутым оригинальным индексам — получается мусор вида `[EMAIL_ADDRESS_xxxxxxxx]a1]`.

**Решение:** `_resolve_conflicts()` убирает перекрывающиеся спаны до замены, оставляя наиболее уверенный (затем самый длинный) спан при каждом конфликте:

```python
def _resolve_conflicts(results):
    by_priority = sorted(results, key=lambda r: (r.score, r.end - r.start), reverse=True)
    kept = []
    for r in by_priority:
        if not any(max(r.start, k.start) < min(r.end, k.end) for k in kept):
            kept.append(r)
    return kept
```

`EMAIL_ADDRESS` (score 1.0) побеждает `URL` (score 0.5) → одна замена, нет искажений.

---

## Восстановление в стриминге — `StreamingRestorer`

Простая замена по чанкам невозможна: `[EMAIL_ADDRESS_3f2a1b0c]` — 26 символов, которые могут прийти разбитыми по нескольким SSE-чанкам. Буферизовать полный ответ перед восстановлением значило бы уничтожить TTFF.

### Алгоритм look-ahead буфера

`StreamingRestorer` держит скользящий буфер (`_buffer: str`) на хвосте потока. Инвариант: всё до буфера уже передано клиенту.

```
входящие чанки → _buffer → _drain() → клиент
```

Цикл `_drain()`:

1. Ищем `[` в `_buffer`. Всё до него безопасно — сразу передаём клиенту.
2. С `[` и далее: вызываем `_probe()`.
3. `_probe()` проверяет:
   - `_buffer` начинается с известного placeholder-а? → восстановить, потребить, повторить.
   - `_buffer` является валидным префиксом какого-либо зарегистрированного placeholder-а? → остановиться, ждать следующего чанка.
   - Иначе → сбросить `[`, сдвинуть на один символ, повторить.

```python
def _probe(self) -> str | None:
    buf = self._buffer
    for placeholder, original in self._map.items():
        if buf.startswith(placeholder):          # полное совпадение
            self._buffer = buf[len(placeholder):]
            return original
    if any(p.startswith(buf) for p in self._map):  # ещё валидный префикс
        if len(buf) > self._max_len:               # защитный ограничитель
            self._buffer = buf[1:]; return "["
        return None                                # ждём следующего чанка
    self._buffer = buf[1:]; return "["             # не placeholder
```

`finalize()` вызывается после завершения потока и сбрасывает остаток буфера как есть (неполный placeholder, который так и не завершился, доходит до клиента без искажений — лучше, чем потерять символы).

### Максимальная задержка

Буфер растёт только пока `_buffer` остаётся валидным префиксом известного placeholder-а. Максимальная задержка = длина самого длинного зарегистрированного placeholder-а — обычно 26 символов (`[EMAIL_ADDRESS_xxxxxxxx]`). Всё остальное проходит немедленно.

### Интеграция в `complete_stream()`

`complete_stream()` теперь зеркалит `complete()`:

```python
context = RestorationContext()
sanitized_messages, input_actions = await self._sanitize_input(..., context=context)
if context.has_replacements():
    sanitized_messages = _inject_system_instruction(sanitized_messages, ...)

restorer = StreamingRestorer(context) if context.has_replacements() else None

async for chunk in adapter.stream_chat(...):
    chunks.append(chunk)               # сырые данные для аудита (с placeholder-ами)
    if restorer:
        safe = restorer.feed(chunk)
        if safe:
            yield safe
    else:
        yield chunk

if restorer:
    tail = restorer.finalize()
    if tail:
        yield tail
```

`chunks` хранит сырое (с placeholder-ами) содержимое для аудита — восстановленный email никогда не попадает в запись аудита.

---

## Что не восстанавливается

**Output sanitizer:** output sanitizer никогда не был в критическом пути для `complete()` (ранее существовавший пробел). Восстановление применяется только к `response.content` от адаптера.

**Заблокированные запросы:** если input sanitizer блокирует запрос, вызов LLM не производится и восстанавливать нечего.

---

## Тестирование

### `tests/unit/test_restoration_context.py` (новый)

| Тест | Что проверяется |
|---|---|
| `test_register_returns_placeholder_with_correct_format` | формат `[TYPE_xxxxxxxx]` |
| `test_register_same_value_returns_same_placeholder` | дедупликация |
| `test_register_different_values_return_different_placeholders` | уникальность |
| `test_restore_replaces_placeholder_with_original` | базовое восстановление |
| `test_restore_multiple_values` | несколько значений в одной строке |
| `test_restore_no_replacements_returns_text_unchanged` | noop при пустом контексте |
| `test_has_replacements_false_initially` | пустой контекст |
| `test_has_replacements_true_after_register` | после регистрации |
| `test_build_system_instruction_is_non_empty_string` | инструкция существует |
| `test_build_system_instruction_describes_pattern` | содержит "placeholder" |

### `tests/unit/test_pii_sanitizer.py` — новые context-aware тесты

| Тест | Что проверяется |
|---|---|
| `test_with_context_registers_replacement` | context заполняется при совпадении |
| `test_with_context_placeholder_in_result` | placeholder появляется в выводе |
| `test_with_context_restore_roundtrip` | sanitize → restore = original |
| `test_with_context_duplicate_value_same_placeholder` | дедупликация через context |
| `test_without_context_uses_fixed_label` | обратная совместимость — формат `[EMAIL]` |

### `tests/unit/test_presidio_sanitizer.py` — новые context-aware и conflict тесты

| Тест | Что проверяется |
|---|---|
| `test_with_context_registers_span_and_replaces` | спан заменён placeholder-ом |
| `test_with_context_restore_roundtrip` | восстановление после санитайзации |
| `test_with_context_two_spans_replaced_correctly` | два неперекрывающихся спана |
| `test_with_context_overlapping_spans_keeps_higher_confidence` | email побеждает url |

### `tests/unit/test_chat_service.py` — новые restoration тесты

| Тест | Что проверяется |
|---|---|
| `test_complete_restores_pii_in_response` | placeholder → original в ответе |
| `test_complete_audit_receives_sanitized_not_restored` | аудит получает версию с placeholder-ами |
| `test_complete_no_restoration_when_no_replacements` | noop когда context пустой |
| `test_complete_injects_system_instruction_when_replacements` | инструкция вставляется |
| `test_complete_no_system_instruction_when_no_replacements` | нет вставки если нет PII |
| `test_complete_stream_restores_pii_in_output` | placeholder восстанавливается по split-чанкам |
| `test_complete_stream_audit_receives_placeholder_not_original` | аудит получает placeholder в стриме |
| `test_complete_stream_injects_system_instruction_when_replacements` | инструкция вставляется в стриме |

### `tests/unit/test_restoration_context.py` — тесты StreamingRestorer

| Тест | Что проверяется |
|---|---|
| `test_streaming_restorer_passthrough_no_brackets` | текст без PII передаётся немедленно |
| `test_streaming_restorer_complete_placeholder_in_one_chunk` | восстановление в одном чанке |
| `test_streaming_restorer_holds_partial_then_restores` | hold-back, затем восстановление |
| `test_streaming_restorer_char_by_char` | placeholder разбит по одному символу |
| `test_streaming_restorer_non_matching_bracket_flushed_immediately` | `[не placeholder]` проходит насквозь |
| `test_streaming_restorer_multiple_placeholders_in_stream` | два placeholder-а в одном потоке |
| `test_streaming_restorer_finalize_flushes_incomplete_buffer` | неполный в конце сбрасывается как есть |
| `test_streaming_restorer_empty_context_is_noop` | нет регистраций → нет буферизации |
