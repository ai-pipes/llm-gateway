# ADR-005: Порядок middleware и privacy-by-design

**Date:** 2026-06-06  
**Status:** Accepted

## Контекст

ASGI middleware стек содержит три слоя: Auth, Sanitize, Audit. Нужно определить порядок их выполнения с учётом двух конкурирующих требований:

1. **Privacy-by-design:** Audit никогда не должен видеть несанитайзированные данные. Это особенно важно для v3, где планируется full request/response logging — он должен логировать уже очищенный текст.
2. **Audit blocked-запросов:** Запросы, заблокированные sanitizer'ом, тоже должны аудироваться со `status=blocked` (compliance-требование — факт попытки передачи данных должен быть зафиксирован).

## Рассмотренные варианты

**A. Auth → Audit → Sanitize → Handler**  
Audit оборачивает Sanitize — запускается для всех аутентифицированных запросов, включая blocked. Но Audit видит запрос ДО санитайзинга. Нарушает privacy-by-design.

**B. Auth → Sanitize → Handler → Audit (post-handler)**  
Audit пишется после Handler через response middleware. Но если Sanitize блокирует запрос ранним `return` (без вызова `call_next`), вся внутренняя цепочка обрывается и Audit не запускается. Нарушает audit-blocked.

**C. Auth → Sanitize → Audit → Handler, где Sanitize при блокировке не прерывает цепочку**  
Sanitize при блокировке сохраняет ошибку в `request.state.blocked_error` и всё равно вызывает `call_next`. Handler проверяет флаг и возвращает 400 самостоятельно. Audit запускается как обёртка вокруг Handler и видит только санитайзированные данные.

## Решение

**C. Auth → Sanitize → Audit → Handler.**

## Обоснование

Вариант C удовлетворяет обоим требованиям:

- Audit стоит после Sanitize в цепочке → видит только санитайзированные данные ✓  
- Sanitize при блокировке не прерывает цепочку → Audit запускается для blocked-запросов ✓  
- Auth делает ранний `return` → Audit не запускается для неаутентифицированных запросов ✓  

Ключевой инсайт: в `BaseHTTPMiddleware` ранний `return` без `call_next` обрывает **всю** внутреннюю цепочку, не только текущий middleware. Поэтому Sanitize не может одновременно и прерывать запрос, и давать Audit возможность запуститься — если они стоят в порядке Sanitize → Audit. Решение: перенести ответственность за возврат 400 из Sanitize в Handler.

## Последствия

- `SanitizeMiddleware` при блокировке не возвращает HTTP-ответ напрямую. Вместо этого записывает в `request.state.blocked_error` и вызывает `call_next`.
- `GatewayHandler` обязан проверять `request.state.blocked_error` первым делом, до разбора тела запроса.
- Порядок вызовов `add_middleware()` в Starlette **обратный** порядку обработки. Чтобы получить Auth → Sanitize → Audit → Handler, регистрируем: `add_middleware(Audit)`, затем `add_middleware(Sanitize)`, затем `add_middleware(Auth)`.
- Любой новый middleware, добавляемый в стек, должен явно решить: прерывает ли он цепочку (ранний `return`) или передаёт управление дальше — и как это влияет на Audit.
