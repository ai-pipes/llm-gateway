# ADR-002: ASGI Middleware Stack как архитектурный паттерн

**Date:** 2026-06-05  
**Status:** Accepted

## Контекст

Нужно организовать pipeline обработки запросов: auth, sanitization, routing, logging. Два варианта: ASGI Middleware Stack vs Chain of Responsibility + Async Event Bus.

## Рассмотренные варианты

**A. ASGI Middleware Stack**  
Каждый concern — отдельный ASGI middleware. Всё синхронно, строго по очереди. Стандарт Python веб-экосистемы (Django, FastAPI).

**B. Chain of Responsibility + Async Event Bus**  
Синхронная цепочка для блокирующих операций (sanitization), асинхронная шина событий для logging/metrics. Лучшая latency.

## Решение

**A. ASGI Middleware Stack.**

## Обоснование

Главный вопрос — что происходит с audit trail при сбое.

В варианте B ответ клиенту уходит до записи аудита. Если event bus упал или очередь переполнена — запись потеряна, но факт передачи данных уже произошёл. Для compliance (финансы, медицина) это неприемлемо.

В варианте A аудит пишется синхронно. Если запись не удалась — клиент получает 500 и ответ LLM не отдаётся. Гарантия: нет записи = нет ответа.

Чтобы сделать B надёжным потребовался бы durable event bus (Redis Streams, Kafka) — существенное усложнение v1 ради latency, которая и так приемлема.

## Последствия

- Latency выше: каждый синхронный middleware добавляет время
- Audit overhead напрямую влияет на response time клиента
- При необходимости async logging — добавляется в v2 отдельным `ObservabilityMiddleware` с fire-and-forget семантикой (метрики не требуют compliance-гарантии)
