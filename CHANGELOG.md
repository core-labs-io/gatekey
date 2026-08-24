# Changelog

All notable changes to Gatekey are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `GET /v1/models` (and `/v1/models/{model}`): OpenAI-compatible model
  discovery for gateway keys, filtered by the caller's effective org+team
  policy — `client.models.list()` now works and shows exactly what that
  key can call.
- `X-Request-ID` on every response (caller-supplied ids honored when
  well-formed), embedded as `error.request_id` in every error body and
  shared with gateway usage-log rows and server log lines.
- Real structured logging: `extra={...}` fields (previously silently
  dropped — no formatter rendered them) now reach the output; `text`
  (key=value) or `json` via `GATEKEY_LOG_FORMAT`.
- Prometheus `/metrics` (HTTP request counters + duration histograms by
  route template) and `/readyz` (real database/Redis checks, 503 when a
  dependency is down); compose healthchecks now use `/readyz`.
- `POST /v1/admin/bootstrap`: one atomic call creating user + team +
  membership (+budget) + service-account key — the four-entity first-key
  chain in one step, with audit entries.
- Streaming failures are now distinguishable from completion: a mid-stream
  provider error emits a structured SSE `{"error": ...}` frame and never
  `data: [DONE]`.
- Budget-exhausted errors carry `budget_usd`/`current_spend_usd` as
  structured fields, not just prose.
- OpenAPI hygiene: stale app description replaced, the error envelope is
  declared on the gateway routes, and a generated `openapi.json` is
  committed at `backend/docs/api/openapi.json` (CI fails on drift).
- Dark mode: full token-based dark palette (system-preference default plus
  an explicit topbar toggle persisted per user, applied before first paint).
- Real spend-over-time chart: SVG line/area chart with hover crosshair +
  tooltip, keyboard navigation, and a "view as table" twin — replaces the
  CSS bar rows on the Dashboard, My Usage, and Org Usage screens.
- DataTable upgrades inherited by every list screen: horizontal scroll
  container, client-side column sorting (aria-sort), text filter, and
  pagination; wired up with sort keys on Users, Service Accounts, and Teams.
- Responsive console: off-canvas sidebar drawer under 900px, fluid stat
  grids, single-column panels, and no horizontal page scroll on mobile.
- Accessibility pass on the shared primitives: modal focus trap +
  Escape-to-close + dialog semantics + focus restore + scroll lock,
  screen-reader-announced toasts with a dismiss button, visible
  :focus-visible styles, associated labels on the provider key form, and
  reduced-motion support.
- Dismissible "Getting started" checklist on the Dashboard, derived from
  live data (provider → user/team → service account → first request).
- Collapsible sidebar nav groups (persisted) and a breadcrumb on the team
  detail page.
- `useApiQuery` hook — the shared load/error/refetch pattern for new
  screens (see docs/development.md).
- GitHub Actions CI: ruff (blocking), mypy (advisory), backend unit +
  integration tests against service Postgres/Redis, frontend
  typecheck+build, cli-sync tests+package build, and a full compose smoke
  test that bootstraps via `setup.sh` and exercises the admin API, gateway
  auth, and console.
- Release workflow: pushing a `vX.Y.Z` tag publishes semver-tagged backend
  and frontend images to GHCR and attaches the `gatekey-sync` sdist/wheel
  to a GitHub release (opt-in PyPI publish via trusted publishing).
- `docker-compose.prod.yml` + Caddy: production deployment on one public
  domain with automatic TLS, same-origin console/API/SCIM routing (no
  CORS), parametrized DB password, no DB/Redis ports on the host, memory
  limits, correct proxy-header settings, and `.env.prod.example`.
- `docs/production.md`: TLS variations, backups + master-key escrow +
  restore, upgrade/rollback, SSO walkthroughs (Okta, Microsoft Entra ID,
  Google Workspace), SCIM setup, cli-sync fleet rollout, and a production
  checklist.
- `cli-sync`: README, `GATEKEY_SYNC_BASE_URL` env var for fleet/MDM
  preconfiguration (flag > env > config > default resolution, with tests),
  and complete package metadata — `python -m build` produces a
  pipx-installable wheel.
- Frontend container healthcheck (both compose files).
- `LICENSE` (Apache-2.0), this changelog, and `CONTRIBUTING.md`.
- `setup.sh` / `setup.ps1` one-command bootstrap: generates both required
  secrets (no host Python needed), writes `.env`, and starts docker-compose.
- `.gitattributes` forcing LF line endings on shell scripts, so a Windows
  clone can't break the backend container's entrypoint.
- `GATEKEY_REDIS_URL` and every other documented variable now present in
  `.env.example`, including the working in-compose Redis value; docker-compose
  now passes `GATEKEY_CORS_ALLOWED_ORIGINS` through to the backend.

### Changed
- The console's browser-facing API base URL is now applied at container
  START (`GATEKEY_PUBLIC_API_BASE_URL`), not baked at image build time —
  one published frontend image works for any backend host. The Next.js
  standalone server now binds 0.0.0.0 explicitly.
- Backend source is now ruff-clean (unused imports removed, a missing
  `TYPE_CHECKING` forward-reference import added, one dead assignment
  removed).
- README restructured around a reader evaluating the project (intro,
  architecture diagram, quick start, feature table); detailed references
  moved to `docs/` (configuration, SSO, console tour, known limitations,
  local development).
- Internal build-phase jargon removed from user-facing surfaces; the admin
  console's "Differentiators" sidebar group is now "AI Oversight".

## [0.1.0] - 2026-08-15

First public cut of Gatekey, a self-hostable enterprise AI gateway. It
proxies OpenAI-compatible API traffic to your own provider keys (OpenAI,
Anthropic, Google Vertex AI, OpenRouter) or self-hosted inference endpoints,
under centrally managed policy. Highlights:

### Core gateway
- OpenAI-compatible `/v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings` with streaming (SSE); switching an app to Gatekey means
  changing only the base URL and API key.
- Provider key management with live validation on entry and AES-256-GCM
  encryption at rest.
- Custom model registry: register a brand-new provider model (with your own
  pricing) the day it ships — no Gatekey release required.
- Per-user, per-team, and org-wide USD budgets with atomic spend accounting
  and hard cutoff; usage dashboard with per-model/per-user breakdowns.

### Governance & identity
- Teams with four server-enforced roles (Org Admin, Auditor, Team Lead,
  Member), join-request onboarding, and nested model policy (teams can only
  narrow the org baseline, never widen it).
- Optional OIDC SSO (any spec-compliant IdP; dev Keycloak bundled for local
  testing), SCIM 2.0 provisioning, personal API keys, and a break-glass
  admin token that always works.
- Append-only audit trail for every governance mutation, with an optional
  tamper-evident hash chain and a `verify` endpoint that pinpoints the exact
  broken entry.

### Security & compliance
- DLP/PII scanning (Presidio, in-process) with log/redact/block actions and
  org-defined custom patterns; content-classification-aware routing across
  four categories (PII, source code, financial data, legal).
- Data-residency rules, scheduled access windows, automatic key rotation,
  and configurable retention.
- Shadow AI discovery via SASE/proxy log ingestion with an allowlist-only
  storage model and a dedicated data-handling policy.

### Reliability & cost
- Multi-key failover with active provider health checks, Redis-backed rate
  limiting (requests + tokens per minute) and exact-match response caching,
  graceful model degradation at budget thresholds, and a cost/reliability
  dashboard with export.

### Deployment
- Single `docker-compose up`: Postgres + backend + frontend, automatic
  migrations, two required secrets, optional `--profile cache` (Redis) and
  `--profile sso` (dev Keycloak).

[Unreleased]: ./CHANGELOG.md
[0.1.0]: ./CHANGELOG.md
