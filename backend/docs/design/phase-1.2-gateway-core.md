---
title: Phase 1.2 — Unified API / Gateway Core — Architecture Design
status: accepted
author: architect
last_updated: 2026-07-14
---

# Phase 1.2 — Unified API / Gateway Core — Design

Scope: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, SSE streaming,
Python/JS SDK examples, per-app service-account keys. Builds directly on Phase 1.1
(`services/provider_keys.py`, `services/encryption.py`, `providers/{base,openai,
anthropic,vertex_ai,registry}.py`, `db/models/{org,provider_key}.py`).

See the full design in the architect's response to the orchestrator (this file is a
durable copy for handoff). Key decisions, translation contracts, auth mechanism,
latency strategy, and task breakdown are reproduced there in full.
