#!/bin/sh
set -e

echo "Gatekey backend: waiting for database and applying migrations..."
# Alembic's own connection retry is minimal; a short external wait loop
# avoids a hard failure if Postgres's own startup (a separate container)
# hasn't finished accepting connections yet by the time this container
# starts - docker-compose's `depends_on: condition: service_healthy` already
# gates this in normal operation, but this loop is cheap, harmless
# belt-and-suspenders for any other orchestration this image gets run under.
ATTEMPTS=0
until alembic upgrade head; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [ "$ATTEMPTS" -ge 30 ]; then
    echo "Gatekey backend: migrations did not succeed after 30 attempts, giving up." >&2
    exit 1
  fi
  echo "Gatekey backend: migration attempt $ATTEMPTS failed, retrying in 2s..."
  sleep 2
done

echo "Gatekey backend: migrations applied, starting server."
exec uvicorn gatekey.main:create_app --factory --host 0.0.0.0 --port 8000
