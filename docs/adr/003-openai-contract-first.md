# ADR-003: OpenAI-compatible API как первичный контракт v1

**Date:** 2026-06-05  
**Status:** Accepted

## Контекст

Нужно выбрать публичный API контракт gateway — формат, в котором клиенты отправляют запросы. Два основных кандидата: OpenAI Messages API (`POST /v1/chat/completions`) и Anthropic Messages API (`POST /v1/messages`).

## Рассмотренные варианты

**A. OpenAI-compatible (`/v1/chat/completions`)**  
Де-факто стандарт индустрии. Поддерживают: OpenAI, Azure OpenAI, Mistral, DeepSeek, vLLM, Ollama, LM Studio и сотни других. Любой OpenAI SDK работает без изменений кода.

**B. Anthropic Messages API (`/v1/messages`)**  
Нативный контракт Claude. Claude Code поддерживает `ANTHROPIC_BASE_URL` для перенаправления трафика. Богаче семантически (tool_use, vision content blocks). Специфические заголовки (`X-Claude-Code-Session-Id`, `X-Claude-Code-Agent-Id`) дают бесплатную корреляцию для audit trail.

## Решение

**A. OpenAI-compatible в v1. B запланирован в v3.**

## Обоснование

OpenAI API стал универсальным стандартом — не только для OpenAI. Де-факто это HTTP-интерфейс для LLM вообще. Выбор A означает нулевые изменения на клиенте для большинства корпоративных инструментов.

Anthropic Messages API важен и войдёт в v3 — особенно для корпораций, использующих Claude Code. `X-Claude-Code-Session-Id` и `X-Claude-Code-Agent-Id` заголовки дают возможность коррелировать запросы по сессиям и агентам без дополнительной инфраструктуры. Это значимая фича для enterprise audit trail.

Откладываем B не потому что он хуже, а потому что v1 должен быть сфокусирован. Добавление второго API контракта удваивает объём работы по тестированию и документации.

## Последствия

- Инструменты, использующие нативный Anthropic SDK без OpenAI-compatible режима, не работают с v1
- В v3 gateway будет поддерживать оба контракта одновременно с routing по `Content-Type` и path
- Адаптеры внутри gateway принимают унифицированный `ChatRequest` — не зависят от внешнего контракта
