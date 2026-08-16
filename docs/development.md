# Local development (without docker-compose)

## Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# Start Postgres yourself (or reuse docker-compose's postgres service), then:
alembic upgrade head
uvicorn gatekey.main:create_app --factory --reload
```

Environment variables for a local backend go in `backend/.env` (template at
`backend/.env.example`); the full reference is
[configuration.md](configuration.md).

### Tests

```bash
pytest tests/unit          # fast, no external dependencies
pytest tests/integration   # spins up a throwaway Postgres container via
                           # Docker automatically, or set
                           # GATEKEY_TEST_DATABASE_URL to point at one you
                           # already have running
```

A handful of Redis-gated tests self-skip cleanly when no Redis instance is
reachable on the default port.

## Frontend

```bash
cd frontend
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE_URL, defaults to http://localhost:8000
npm run dev
```

Typecheck and production build (both must stay clean):

```bash
npx tsc --noEmit
npm run build
```

Frontend conventions:

- **Data fetching**: new screens use the `useApiQuery` hook
  (`src/lib/useApiQuery.ts`) instead of hand-rolling
  useEffect + loading/error state; convert existing pages opportunistically
  when touching them.
- **Tables**: use the shared `DataTable` and pass `sortValue` per column
  plus `searchText` so sorting/filtering/pagination come for free.
- **Charts**: use the primitives in `src/components/charts.tsx` (themed via
  the `--chart-*` CSS tokens); every chart keeps a table-view twin so no
  value is hover-gated.
- **Colors**: never hardcode a hex in a component — add a token to
  `app/globals.css` and define it in all three theme blocks (light, the
  prefers-color-scheme dark block, and `[data-theme="dark"]`).

## SSO against a locally-running backend

Start just the IdP with `docker compose --profile sso up keycloak` and use
`GATEKEY_OIDC_ISSUER_URL=http://localhost:8080/realms/gatekey-dev` — the
host-published port, not the in-compose service name (that form is only
correct when the backend itself runs inside compose). Full SSO walkthrough:
[sso.md](sso.md).

## cli-sync (the `gatekey-sync` CLI)

`cli-sync/` is a standalone helper that keeps a rotated personal API key
synced to a local CLI tool via the OS keychain. It is installed and run
separately, not part of docker-compose — see
[../cli-sync/README.md](../cli-sync/README.md) for usage and fleet
rollout. For development:

```bash
pip install -e cli-sync/ pytest
pytest cli-sync/tests
python -m build cli-sync    # sdist + wheel (pip install build first)
```

## Design and requirement documents

- `gatekey/` — the product requirement documents the features were built
  from (kept as historical source of scope).
- `backend/docs/design/` — per-feature architecture/design docs and
  security reviews.
- `backend/docs/compliance/` — the data-flow diagram and data-handling
  policy, written for customer security reviews.
- `backend/examples/` — drop-in Python and JavaScript examples for
  switching an app from a provider SDK to Gatekey.
