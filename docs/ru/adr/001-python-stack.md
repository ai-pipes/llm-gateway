# ADR-001: Python как основной стек

**Date:** 2026-06-05  
**Status:** Accepted

## Контекст

Нужно выбрать язык/стек для gateway. Рассматривались Go, TypeScript, Python, Rust.

## Рассмотренные варианты

| Вариант | Latency | Ecosystem | Сложность |
|---|---|---|---|
| Rust | <1ms P99 | минимальный | высокая |
| Go | ~11µs | средний | низкая |
| TypeScript | умеренная | огромный (npm) | низкая |
| Python | ~8ms P95 | AI/ML-rich | низкая |

## Решение

**Python.**

## Обоснование

1. **Образовательный контекст.** Проект учит AI engineering — Python доминирует в этой экосистеме. Читаемость и знакомость важнее latency.
2. **Будущие sanitizer'ы.** NLP-библиотеки для PII detection (spaCy, Microsoft Presidio) — Python-first.
3. **Latency приемлема.** 8ms overhead не критичен для корпоративного use case, где реальные LLM вызовы занимают 500-5000ms.

## Последствия

- GIL ограничивает throughput при CPU-heavy операциях (NLP sanitization)
- PyPI dependency risks — минимизируем зависимости, используем только хорошо известные пакеты
- При необходимости scale — можно запустить несколько uvicorn workers или перейти на Go в будущем
