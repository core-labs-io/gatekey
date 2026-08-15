#!/usr/bin/env sh
# Gatekey one-command setup (Linux / macOS / Git Bash).
#
#   ./setup.sh            # generate secrets, write .env, start docker compose
#   ./setup.sh --cache    # also start Redis and enable rate limiting/caching
#   ./setup.sh --sso      # also start the bundled dev-only Keycloak IdP
#   ./setup.sh --no-start # only generate .env, don't start containers
#
# Requires: Docker (with the compose plugin or docker-compose). No Python,
# no other host dependencies.
set -eu

CACHE=0
SSO=0
START=1
for arg in "$@"; do
  case "$arg" in
    --cache) CACHE=1 ;;
    --sso) SSO=1 ;;
    --no-start) START=0 ;;
    -h|--help)
      sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 1
      ;;
  esac
done

cd "$(dirname "$0")"

fail() { echo "ERROR: $1" >&2; exit 1; }

[ -f .env.example ] || fail ".env.example not found - run this from the Gatekey repo root."

if [ -f .env ]; then
  fail ".env already exists - refusing to overwrite it.
       To start over, delete .env and re-run. To just start the stack:
       docker compose up -d --build"
fi

command -v docker >/dev/null 2>&1 || fail "Docker is not installed (or not on PATH). Install Docker first: https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || fail "The Docker daemon isn't running. Start Docker (e.g. Docker Desktop) and re-run."

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  fail "Neither 'docker compose' nor 'docker-compose' is available."
fi

# --- Generate the two required secrets (no host Python needed) -------------
if command -v openssl >/dev/null 2>&1; then
  ADMIN_TOKEN="$(openssl rand -hex 32)"
  MASTER_KEY="$(openssl rand -base64 32)"
else
  # /dev/urandom fallback; od is POSIX, base64 ships everywhere Docker does.
  ADMIN_TOKEN="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
  MASTER_KEY="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
fi
[ -n "$ADMIN_TOKEN" ] && [ -n "$MASTER_KEY" ] || fail "Secret generation failed."

# --- Write .env from the template -------------------------------------------
cp .env.example .env
# base64 can contain + / = ; '|' as the sed delimiter avoids collisions.
sed "s|^GATEKEY_ADMIN_TOKEN=.*|GATEKEY_ADMIN_TOKEN=${ADMIN_TOKEN}|" .env > .env.tmp && mv .env.tmp .env
sed "s|^GATEKEY_MASTER_KEY=.*|GATEKEY_MASTER_KEY=${MASTER_KEY}|" .env > .env.tmp && mv .env.tmp .env

PROFILES=""
if [ "$CACHE" = 1 ]; then
  # Enabling Redis takes both the profile AND the URL - do both here so the
  # features actually turn on.
  sed "s|^# GATEKEY_REDIS_URL=redis://redis:6379/0|GATEKEY_REDIS_URL=redis://redis:6379/0|" .env > .env.tmp && mv .env.tmp .env
  PROFILES="$PROFILES --profile cache"
fi
if [ "$SSO" = 1 ]; then
  for var in "GATEKEY_OIDC_ISSUER_URL=http://keycloak:8080/realms/gatekey-dev" \
             "GATEKEY_OIDC_CLIENT_ID=gatekey-backend" \
             "GATEKEY_OIDC_CLIENT_SECRET=gatekey-dev-client-secret" \
             "GATEKEY_OIDC_REDIRECT_URI=http://localhost:8000/v1/auth/sso/callback" \
             "GATEKEY_SESSION_COOKIE_SECURE=false"; do
    sed "s|^# ${var}|${var}|" .env > .env.tmp && mv .env.tmp .env
  done
  PROFILES="$PROFILES --profile sso"
fi

echo ""
echo "Wrote .env with freshly generated secrets."
echo ""
echo "  Admin token (sign in to the console with this):"
echo "    ${ADMIN_TOKEN}"
echo ""
echo "  IMPORTANT: back up the GATEKEY_MASTER_KEY value in .env somewhere"
echo "  safe (password manager / secrets vault). If it is lost, every"
echo "  provider key stored in the database becomes permanently"
echo "  unrecoverable."
echo ""

if [ "$START" = 0 ]; then
  echo "Skipping container start (--no-start). Start later with:"
  echo "  $COMPOSE$PROFILES up -d --build"
  exit 0
fi

echo "Building and starting containers (first build takes a few minutes)..."
# shellcheck disable=SC2086  # PROFILES is deliberately word-split
$COMPOSE $PROFILES up -d --build

printf "Waiting for the backend to become healthy"
i=0
until [ $i -ge 90 ]; do
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; then break; fi
  else
    if $COMPOSE ps backend 2>/dev/null | grep -q healthy; then break; fi
  fi
  printf "."
  sleep 2
  i=$((i + 1))
done
echo ""
if [ $i -ge 90 ]; then
  fail "Backend did not become healthy within 3 minutes. Check logs with: $COMPOSE logs backend"
fi

echo ""
echo "Gatekey is running."
echo ""
echo "  Admin console:  http://localhost:3000   (sign in with the admin token above)"
echo "  Gateway API:    http://localhost:8000"
[ "$SSO" = 1 ] && echo "  Dev Keycloak:   http://localhost:8080   (admin/admin; test user: testuser/testpassword)"
echo ""
echo "Next: open the console, connect your first provider key, then follow"
echo "the Quick start in README.md to make your first proxied request."
