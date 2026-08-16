#!/bin/sh
# Gatekey console entrypoint: make the browser-facing backend URL a RUNTIME
# setting instead of a build-time bake.
#
# Next.js inlines NEXT_PUBLIC_* variables into the client bundle at build
# time, which would force a rebuild to point a published image at a
# different backend host. Instead, the image is built with a fixed
# placeholder string (see Dockerfile) and this entrypoint substitutes the
# real URL into the built output on container start.
#
# One-shot substitution is safe: a Docker container's environment is
# immutable for its lifetime (changing the env means recreating the
# container, which starts again from the image's pristine placeholder).
set -eu

PLACEHOLDER="__GATEKEY_API_BASE_URL__"
URL="${GATEKEY_PUBLIC_API_BASE_URL:-http://localhost:8000}"

case "$URL" in
  *"|"*)
    echo "ERROR: GATEKEY_PUBLIC_API_BASE_URL must not contain '|'" >&2
    exit 1
    ;;
esac
# Escape sed-replacement specials (& and \) so any legal URL is safe.
ESCAPED=$(printf '%s' "$URL" | sed 's/[&\\]/\\&/g')

# Replace in every built file that contains the placeholder (client chunks,
# prerendered HTML, RSC payloads, and the standalone server bundle alike).
grep -rlZ "$PLACEHOLDER" /app 2>/dev/null | xargs -0 -r sed -i "s|$PLACEHOLDER|$ESCAPED|g"

echo "Gatekey console: backend API base URL set to $URL"
exec node server.js
