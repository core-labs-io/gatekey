# Contributing to Gatekey

Thanks for your interest in improving Gatekey. This guide covers local
setup, how to run the test suites, and what a good change looks like.

## Project layout

| Path | What it is |
|---|---|
| `backend/` | FastAPI gateway + admin API (Python 3.11+, SQLAlchemy, Alembic) |
| `frontend/` | Admin & user console (Next.js 14, strict TypeScript) |
| `cli-sync/` | `gatekey-sync` CLI — keeps a rotated personal API key synced to local tools |
| `devops/` | Dev-only Keycloak realm for SSO testing |
| `docs/` | Operator documentation |
| `gatekey/` | Product requirement documents (historical source of scope) |

## Local development

See [docs/development.md](docs/development.md) for full backend/frontend
setup without docker-compose. Short version:

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
alembic upgrade head        # needs a running Postgres
uvicorn gatekey.main:create_app --factory --reload

# Frontend
cd frontend
npm install
npm run dev
```

## Running tests

```bash
cd backend
pytest tests/unit           # fast, no external dependencies
pytest tests/integration    # spins up a throwaway Postgres via Docker

cd frontend
npx tsc --noEmit            # typecheck
npm run build               # production build must stay clean
```

All backend tests and the frontend typecheck/build must pass before a
change is considered done. New behavior needs new tests — bug fixes should
include a regression test that fails without the fix.

## Ground rules for changes

These are the project's standing non-negotiables; changes that violate them
won't be accepted:

- **Self-hosted first.** No phone-home telemetry. Anything that sends data
  outside the deployment boundary must be strictly opt-in.
- **No plaintext secrets at rest or in logs.** Provider keys, webhook URLs,
  and bearer tokens are encrypted (AES-256-GCM) or stored as hashes —
  follow the existing patterns in `backend/src/gatekey/services/encryption.py`.
- **Server-side enforcement.** The UI hiding a control is never the only
  guard; every privileged action is authorized in the backend.
- **The UI's OpenAI-compatible surface stays compatible.** Don't break
  drop-in SDK usage.
- **Migrations must be reversible.** Every Alembic migration needs a working
  `downgrade()`, verified against a real Postgres.
- **Flag, never silently auto-resolve.** When two configurations conflict,
  block or surface the conflict — don't pick a winner quietly.

## Submitting a change

1. Fork/branch from `main`.
2. Keep the change focused; unrelated refactors belong in their own PR.
3. Run the test suites above.
4. Update documentation that your change makes stale (`docs/`, `README.md`,
   `.env.example`) and add a line to `CHANGELOG.md` under **Unreleased**.
5. Open a pull request describing what changed and why, including any
   deliberate limitations.

## Reporting security issues

Please do not open a public issue for suspected vulnerabilities in secret
handling, authentication/RBAC, DLP, or the audit trail. Contact the
maintainers privately with details and reproduction steps.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
