# ADR-003: OpenAI-Compatible API as the Primary v1 Contract

**Date:** 2026-06-05  
**Status:** Accepted

## Context

We needed to choose the public API contract for the gateway — the format in which clients send requests. Two main candidates were considered: the OpenAI Messages API (`POST /v1/chat/completions`) and the Anthropic Messages API (`POST /v1/messages`).

## Options Considered

**A. OpenAI-compatible (`/v1/chat/completions`)**  
The de-facto industry standard. Supported by: OpenAI, Azure OpenAI, Mistral, DeepSeek, vLLM, Ollama, LM Studio, and hundreds of others. Any OpenAI SDK works without code changes.

**B. Anthropic Messages API (`/v1/messages`)**  
The native Claude contract. Claude Code supports `ANTHROPIC_BASE_URL` for traffic redirection. Semantically richer (tool_use, vision content blocks). Proprietary headers (`X-Claude-Code-Session-Id`, `X-Claude-Code-Agent-Id`) provide free correlation for the audit trail.

## Decision

**A. OpenAI-compatible in v1. B is planned for v3.**

## Rationale

The OpenAI API has become a universal standard — not just for OpenAI. It is effectively the HTTP interface for LLMs in general. Choosing option A means zero client-side changes for most enterprise tools.

The Anthropic Messages API is important and will be included in v3 — especially for organizations using Claude Code. The `X-Claude-Code-Session-Id` and `X-Claude-Code-Agent-Id` headers make it possible to correlate requests by session and agent without additional infrastructure. This is a valuable feature for enterprise audit trails.

We are deferring option B not because it is inferior, but because v1 must stay focused. Adding a second API contract doubles the testing and documentation workload.

## Consequences

- Tools using the native Anthropic SDK without an OpenAI-compatible mode will not work with v1
- In v3, the gateway will support both contracts simultaneously, routing by `Content-Type` and path
- Internal gateway adapters operate on a unified `ChatRequest` and are decoupled from the external contract
