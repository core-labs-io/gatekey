# SSO (single sign-on) setup

SSO is entirely opt-in. With it, real people sign in through your identity
provider instead of sharing the admin token: each user gets an individual
identity, a role, self-service personal API keys, and their actions show up
attributably in the audit log. Without it, the admin token remains the only
auth path and the SSO routes simply return 404.

## Configuration

Four env vars enable SSO, and they are all-or-none — set all four or none;
the backend fails fast at startup on a partial set:

```
GATEKEY_OIDC_ISSUER_URL      # e.g. https://your-tenant.okta.com or http://keycloak:8080/realms/gatekey-dev
GATEKEY_OIDC_CLIENT_ID
GATEKEY_OIDC_CLIENT_SECRET   # Gatekey is a confidential client - the browser never sees this
GATEKEY_OIDC_REDIRECT_URI    # http://<backend-host>:8000/v1/auth/sso/callback
```

Two session vars tune the resulting cookie:

```
GATEKEY_SESSION_COOKIE_SECURE   # default true. Set false ONLY for local http dev -
                                # otherwise the browser never sends the cookie over http
                                # and every SSO login appears to silently fail.
GATEKEY_SESSION_TTL_HOURS       # default 12
```

## Trying it locally with the bundled Keycloak

A dev-only Keycloak IdP ships in `docker-compose.yml` behind the `sso`
profile — plain `docker compose up` never starts it. The shortcut is
`./setup.sh --sso` (or `.\setup.ps1 -Sso`), which uncomments the right
`.env` values and starts the profile in one step. The manual path:

> **WARNING — dev-only credentials.** The checked-in Keycloak admin login
> (`admin`/`admin`), the realm's fixed client secret
> (`devops/keycloak/gatekey-realm.json`), and the seeded test user are for
> local development and testing **only**. Never expose this Keycloak
> container publicly and never front a production Gatekey deployment with
> it — for production, point the `GATEKEY_OIDC_*` vars at your real IdP
> with a real client secret.

1. In `.env`, uncomment the SSO block (the values are pre-filled to match
   the checked-in realm):

   ```
   GATEKEY_OIDC_ISSUER_URL=http://keycloak:8080/realms/gatekey-dev
   GATEKEY_OIDC_CLIENT_ID=gatekey-backend
   GATEKEY_OIDC_CLIENT_SECRET=gatekey-dev-client-secret
   GATEKEY_OIDC_REDIRECT_URI=http://localhost:8000/v1/auth/sso/callback
   GATEKEY_SESSION_COOKIE_SECURE=false
   ```

   Note the issuer host is `keycloak:8080` (the in-compose service name) —
   the backend reaches Keycloak container-to-container, while Keycloak's
   own hostname config keeps browser redirects on `localhost:8080`, so both
   work at once. Only if you run the backend *outside* compose (local dev
   against `--profile sso` Keycloak alone) should the issuer be
   `http://localhost:8080/realms/gatekey-dev` instead.

2. Start everything including Keycloak:

   ```bash
   docker compose --profile sso up --build
   ```

   Keycloak imports the `gatekey-dev` realm on startup: OIDC client
   `gatekey-backend` and one seeded test user, **`testuser`** /
   **`testpassword`**. The Keycloak admin console is at
   `http://localhost:8080` (`admin`/`admin`) if you want to add more test
   users — you'll need at least two users to exercise the Team Lead
   approval flow end-to-end.

3. Open `http://localhost:3000` — the login screen now shows a
   "Sign in with SSO" button above the admin-token field (it probes the
   backend and only appears when SSO is actually configured). Sign in as
   `testuser`/`testpassword`.

4. **First login lands on onboarding**, not the console: a brand-new SSO
   user has no role and no team. They confirm their name, pick a team, and
   submit a join request (one pending request per user at a time), then sit
   on a holding screen until it's decided. A Team Lead of that team sees
   the request under **My Team → Join Requests** and approves it with a
   budget in one step; if the team has no Team Lead (or the request has
   been pending five business days), it appears in the Org Admin queue
   instead — the break-glass admin session can approve it from the team's
   detail page on the **Teams** screen.

5. **Granting org-wide roles** (Org Admin, Auditor) currently has **no
   console UI** — it's API-only. Find the user's id on the Users screen,
   then:

   ```bash
   curl -X PATCH http://localhost:8000/v1/admin/users/<user-id>/org-role \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"org_role": "org_admin"}'    # or "auditor", or null to clear
   ```

   Team-level roles (Team Lead / Member) *are* editable in the UI, on the
   team's members table.

6. Once approved, the user lands in the non-admin console (see
   [console.md](console.md)), can mint a personal key under **My API Keys**
   (`gk_pk_...`, plaintext shown once), and can call the gateway with it
   exactly like a service-account key.

## Production IdPs (Okta, Azure AD, Google Workspace, ...)

The flow is a standard, provider-agnostic OIDC authorization-code flow
(discovery document, PKCE, `sub` claim as the durable user identifier), so
any spec-compliant IdP should work by pointing the four `GATEKEY_OIDC_*`
vars at it: register Gatekey as a **confidential** web client with the
callback URL `https://<your-backend>/v1/auth/sso/callback` and scopes
`openid profile email`.

**Only Keycloak has actually been exercised end-to-end** — Okta / Azure AD /
Google Workspace are structurally compatible but were not live-verified (no
real IdP tenant was available in the environment this code was produced in).
The Identity & Access screen's "Test connection" button does a live
discovery-document fetch against your configured issuer, which is a quick
first sanity check. Verify a full login round-trip against your real IdP
before relying on it, and report back what you find.

The same caveat applies to SCIM provisioning: the endpoints follow the SCIM
2.0 RFC and are covered by integration tests, but no live provisioning
round-trip from an actual IdP has been run. See
[known-limitations.md](known-limitations.md).
