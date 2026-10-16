#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -f docker-compose.override.yml ]; then
    exec docker compose -f docker-compose.yml -f docker-compose.dashboard.yml -f docker-compose.override.yml "$@"
fi
exec docker compose -f docker-compose.yml -f docker-compose.dashboard.yml "$@"
