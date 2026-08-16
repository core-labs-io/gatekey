# Production deployment guide

This guide covers running Gatekey for an organization: TLS, backups,
upgrades/rollback, SSO against real identity providers, SCIM provisioning,
and rolling out the `gatekey-sync` CLI. For a laptop evaluation, use the
quick start in the main README instead.

## Architecture

`docker-compose.prod.yml` runs five containers on one host:

```
internet ── :80/:443 ──> Caddy (automatic TLS)
                           ├── /v1/*, /scim/v2/*, /healthz ──> backend :8000
                           └── everything else ─────────────> console :3000
                         backend ──> Postgres (internal only)
                                └──> Redis (optional, internal only)
```

Everything shares **one public domain** — console, API, SSO callback, and
SCIM — so there is no CORS configuration and no cross-origin cookie
trouble. Postgres and Redis are not reachable from outside the compose
network; Caddy is the only ingress.

## Prerequisites

- A Linux host with Docker (compose v2 included).
- A DNS record for your chosen domain (e.g. `gatekey.example.com`)
  pointing at the host.
- Ports **80 and 443** reachable from the internet (Let's Encrypt uses
  them to issue and renew the certificate automatically). If the host
  can't be internet-reachable, see "TLS variations" below.

## Install

```bash
git clone <this-repo> gatekey && cd gatekey
cp .env.prod.example .env
# Fill in the four required values - the file documents each one:
#   GATEKEY_DOMAIN, GATEKEY_DB_PASSWORD, GATEKEY_ADMIN_TOKEN, GATEKEY_MASTER_KEY
docker compose -f docker-compose.prod.yml up -d --build
```

`--build` builds the images from the checkout. To use published images
instead, pin them in `.env` and omit `--build`:

```
GATEKEY_BACKEND_IMAGE=ghcr.io/core-labs-io/gatekey-backend:0.1.0
GATEKEY_FRONTEND_IMAGE=ghcr.io/core-labs-io/gatekey-frontend:0.1.0
```

Always pin an exact version in production — never `latest` — so upgrades
happen when you decide, and rollback is a tag change.

Within a minute or two, `https://<your-domain>` serves the console with a
real certificate. Sign in with `GATEKEY_ADMIN_TOKEN` and follow the quick
start from step "connect your first provider".

**Before onboarding anyone: escrow `GATEKEY_MASTER_KEY`** (see Backups).

## TLS variations

- **Default (recommended):** Caddy + Let's Encrypt, fully automatic
  issuance and renewal. Nothing to do.
- **Behind an existing load balancer / reverse proxy** that terminates TLS
  for you: you don't need the bundled Caddy. Point your proxy at the
  backend (`:8000`) for `/v1/*`, `/scim/v2/*`, `/healthz` and the frontend
  (`:3000`) for everything else, publish those ports in your own compose
  override, and keep `GATEKEY_TRUST_PROXY_HEADERS=true` **only if** your
  proxy overwrites `X-Forwarded-For` on the way in. Keep console and API
  on one hostname to preserve the same-origin setup.
- **Internal CA / custom certificates:** replace the site block in
  `devops/caddy/Caddyfile` with a
  [`tls cert.pem key.pem`](https://caddyserver.com/docs/caddyfile/directives/tls)
  directive and mount your certificate files into the caddy container.

## Backups

Three things constitute a full Gatekey backup. Losing #2 makes #1
partially unrecoverable — treat them as a set.

1. **The database.** Nightly `pg_dump` (adjust retention to your policy):

   ```bash
   docker compose -f docker-compose.prod.yml exec -T postgres \
     pg_dump -U gatekey -d gatekey --format=custom \
     > gatekey-$(date +%F).dump
   ```

2. **The master key** (`GATEKEY_MASTER_KEY` in `.env`). Escrow it in your
   organization's secrets vault or password manager **now**. Every
   provider key, self-hosted bearer token, and webhook URL in the database
   is AES-256-GCM-encrypted under this key; without it a database restore
   yields rows that can never be decrypted, and every provider secret has
   to be re-entered by hand. There is no recovery path by design.

3. **The `.env` file itself** (admin token, DB password, OIDC/SMTP
   settings) — or the ability to regenerate it from your vault.

The Caddy volumes (`gatekey_caddy_data`) only hold certificates; they
re-issue automatically after a loss and don't need backing up.

**Restore procedure** (fresh host):

```bash
git clone <this-repo> gatekey && cd gatekey
# restore .env from your vault - SAME master key, or the data is lost
docker compose -f docker-compose.prod.yml up -d postgres
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore -U gatekey -d gatekey --clean --if-exists < gatekey-YYYY-MM-DD.dump
docker compose -f docker-compose.prod.yml up -d
```

Verify with a test request and a look at the audit log before pointing
users at it. Note the restore's audit-trail implications if you use the
hash-chained ledger: restoring an older dump discards entries written
after that dump (the chain stays internally valid — see the
tail-truncation limitation in [known-limitations.md](known-limitations.md)).

## Upgrades

Migrations run automatically on backend start (`alembic upgrade head` in
the container entrypoint), so an upgrade is:

```bash
# 1. Take a database backup first (see above) - migrations are one-way in
#    practice; the backup is your rollback path for schema changes.
# 2a. Pinned published images: bump the two image tags in .env, then
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
# 2b. Or, building from source:
git pull && docker compose -f docker-compose.prod.yml up -d --build
```

Watch `docker compose -f docker-compose.prod.yml logs -f backend` through
the first start — migration output appears there — and confirm
`https://<domain>/healthz` returns 200.

**Rollback:** revert the image tags (or `git checkout` the previous tag)
and `up -d` again. If the newer version's migrations already ran, restore
the pre-upgrade database dump as well — do not run an older application
against a newer schema.

## SSO against a production IdP

The flow is standard OIDC authorization-code (discovery document, PKCE,
confidential client). Full mechanics: [sso.md](sso.md). For this
deployment the values are:

- **Redirect URI:** `https://<GATEKEY_DOMAIN>/v1/auth/sso/callback`
- **Scopes:** `openid profile email`
- **Client type:** confidential (Gatekey holds the secret server-side)

Set the four `GATEKEY_OIDC_*` variables in `.env` and
`docker compose -f docker-compose.prod.yml up -d` to apply. The console's
**Identity & Access → Test connection** button live-fetches your issuer's
discovery document as a first sanity check.

> **Caveat, stated honestly:** only Keycloak has been exercised
> end-to-end. The walkthroughs below follow each vendor's standard OIDC
> app registration and are structurally correct, but menu names drift and
> no live tenant round-trip has been run — verify a full login before
> rolling out, and please report what you find.

### Okta

1. Admin console → **Applications → Create App Integration** →
   **OIDC - OpenID Connect** → **Web Application**.
2. Sign-in redirect URI: `https://<domain>/v1/auth/sso/callback`. Assign
   the users/groups who should reach Gatekey.
3. From the app's General tab take the **Client ID** and **Client
   secret**. Your issuer is your Okta org URL — typically
   `https://<yourorg>.okta.com` (or its custom authorization server,
   `https://<yourorg>.okta.com/oauth2/default`, if you use one; the
   issuer must serve `<issuer>/.well-known/openid-configuration`).
4. `.env`:
   ```
   GATEKEY_OIDC_ISSUER_URL=https://<yourorg>.okta.com
   GATEKEY_OIDC_CLIENT_ID=<client id>
   GATEKEY_OIDC_CLIENT_SECRET=<client secret>
   GATEKEY_OIDC_REDIRECT_URI=https://<domain>/v1/auth/sso/callback
   ```

### Microsoft Entra ID (Azure AD)

1. Entra admin center → **App registrations → New registration**.
   Platform **Web**, redirect URI `https://<domain>/v1/auth/sso/callback`.
2. **Certificates & secrets → New client secret** — note the secret
   *value* (shown once).
3. Issuer is tenant-scoped:
   `https://login.microsoftonline.com/<tenant-id>/v2.0`.
4. `.env`:
   ```
   GATEKEY_OIDC_ISSUER_URL=https://login.microsoftonline.com/<tenant-id>/v2.0
   GATEKEY_OIDC_CLIENT_ID=<application (client) id>
   GATEKEY_OIDC_CLIENT_SECRET=<client secret value>
   GATEKEY_OIDC_REDIRECT_URI=https://<domain>/v1/auth/sso/callback
   ```

### Google Workspace

1. Google Cloud console → **APIs & Services → Credentials → Create
   credentials → OAuth client ID** → type **Web application**.
2. Authorized redirect URI: `https://<domain>/v1/auth/sso/callback`.
   Configure the OAuth consent screen as **Internal** so only your
   Workspace users can sign in.
3. `.env`:
   ```
   GATEKEY_OIDC_ISSUER_URL=https://accounts.google.com
   GATEKEY_OIDC_CLIENT_ID=<client id>.apps.googleusercontent.com
   GATEKEY_OIDC_CLIENT_SECRET=<client secret>
   GATEKEY_OIDC_REDIRECT_URI=https://<domain>/v1/auth/sso/callback
   ```

## SCIM provisioning

Gatekey exposes a SCIM 2.0 server for automated user provisioning/
deprovisioning from your IdP (endpoints follow the RFC; covered by
integration tests; no live IdP round-trip has been run — same caveat
discipline as SSO).

1. Enable it and mint the bearer token (Org Admin):

   ```bash
   curl -X PUT https://<domain>/v1/admin/scim-config \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"enabled": true}'
   ```

   The response includes the SCIM base URL
   (`https://<domain>/scim/v2`) and a bearer token, **shown once** —
   it's stored hashed. `POST /v1/admin/scim-config/rotate-token` mints a
   replacement.

2. In your IdP's provisioning section (Okta: the app's "Provisioning"
   tab; Entra: "Provisioning" on the enterprise application), set:
   - **SCIM connector base URL / Tenant URL:** `https://<domain>/scim/v2`
   - **Authentication:** HTTP header, `Authorization: Bearer <token>`
3. Start with a small assignment group and verify created users appear on
   the console's Users screen before scaling out.

## Rolling out gatekey-sync (developer key sync)

`gatekey-sync` keeps each developer's personal API key current through
automatic rotation. See [../cli-sync/README.md](../cli-sync/README.md)
for full usage. For a fleet:

1. Distribute the wheel from the GitHub release (or your internal PyPI
   mirror): `pipx install gatekey_sync-<version>-py3-none-any.whl`.
2. Preconfigure the gateway URL machine-wide via MDM/login script:
   `GATEKEY_SYNC_BASE_URL=https://<domain>` — users then only run
   `gatekey-sync login` once.

## Monitoring

- `https://<domain>/healthz` — liveness: the process is up.
- `/readyz` — readiness: verifies the database (and Redis, when
  configured) actually respond; returns 503 with per-check detail
  otherwise. The compose healthchecks use it, so a backend with a dead
  dependency never reports healthy.
- `/metrics` — Prometheus exposition: `gatekey_http_requests_total` and
  `gatekey_http_request_duration_seconds`, labeled by method, route
  template, and status, plus standard process metrics.
- **`/readyz` and `/metrics` are deliberately NOT routed by the bundled
  Caddyfile** — they're for inside-the-network scrapers/orchestrators.
  Point Prometheus at the backend container directly (`backend:8000`), or
  add an access-controlled route in `devops/caddy/Caddyfile` if you need
  them externally.
- **Log correlation:** every response carries an `X-Request-ID` header
  (honored from the caller when supplied), every error body embeds the
  same id as `error.request_id`, and gateway usage-log rows/server log
  lines share it. Set `GATEKEY_LOG_FORMAT=json` for pipeline-friendly
  structured logs (see [configuration.md](configuration.md)).
- `docker compose -f docker-compose.prod.yml logs -f backend` — request
  and scheduler logs, migration output on start.
- The console dashboard tracks spend, error rate, latency, cache hit
  rate, and failover events.

## Production checklist

- [ ] `GATEKEY_MASTER_KEY` escrowed in a vault (not only in `.env`)
- [ ] Nightly `pg_dump` scheduled and restore-tested once
- [ ] Image tags pinned to an exact version
- [ ] `GATEKEY_ADMIN_TOKEN` stored like a root credential; day-to-day
      admin work happens through SSO Org Admin accounts instead
- [ ] SSO verified with a full login round-trip; break-glass token kept
      for emergencies
- [ ] DB password is unique to this deployment (it's inside the compose
      network only, but defense in depth is cheap)
- [ ] Provider pricing table sanity-checked against current provider
      pricing (see [known-limitations.md](known-limitations.md))
- [ ] If using rate limits: every `tokens_per_min` rule also sets
      `requests_per_min` (burst-bound caveat in known-limitations.md)
