# ADR-006: Transparent Tools Passthrough — Client-Owned Tool Schemas

**Date:** 2026-06-12  
**Status:** Accepted

## Context

The gateway proxies chat requests to upstream LLMs. Many clients — MCP adapters, agent frameworks, coding assistants — rely on the OpenAI `tools` API. We needed to decide how the gateway handles tool schemas: who defines them, where they live, and what the gateway does with `tool_calls` in responses.

## Options Considered

**A. Gateway-side tool registry**  
The gateway maintains a registry of known tools (Sentry MCP, internal APIs, etc.) and injects the appropriate schemas based on client identity or config. The client sends a plain message; the gateway adds tools before forwarding to the LLM.

**B. Transparent passthrough — client sends tool schemas (chosen)**  
The client sends the full OpenAI-compatible `tools` array with each request. The gateway forwards it to the LLM without inspection. The LLM returns `tool_calls`; the gateway returns them to the client. The client executes the tool and sends the next request with a `role: "tool"` message.

## Decision

**B. Transparent passthrough.**

## Rationale

Option A makes the gateway a registry — every new tool or MCP adapter requires a gateway deploy. Tool schemas drift from actual implementations over time. The gateway accumulates business logic that belongs to the clients that use those tools.

Option B keeps the gateway a thin proxy. The principle is the same as with adapters and sanitizers: the gateway defines the extension points, clients bring their own implementations. A Sentry MCP adapter is a separate service that manages its own tool schemas; it communicates with the gateway over a standard OpenAI-compatible interface. Zero gateway changes when a new tool is added or an existing one changes its schema.

The gateway does not run an agentic loop. It returns `tool_calls` to the client and waits for the next request. This matches the OpenAI contract and keeps the gateway stateless.

## Consequences

- The gateway treats `tools` as an opaque blob — no schema validation, no business logic
- `tool_calls` in responses are returned verbatim to the client after PII restoration (see below)
- The client is responsible for tool execution and managing the conversation loop
- PII placeholders may appear inside `tool_call` arguments if the LLM echoes them — the gateway restores original values before returning to the client (both streaming and non-streaming)
- `tool_choice` passthrough is trivially addable but deferred until a client requires it
- Sanitizing `function.arguments` for PII on input is out of scope until real usage reveals whether it is needed
