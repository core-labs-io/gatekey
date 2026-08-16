# Changelog

All notable changes to Gatekey are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
