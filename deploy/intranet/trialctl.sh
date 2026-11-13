#!/bin/sh
set -eu
umask 077
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$root"
compose="$HOME/.local/bin/docker-compose"
case "${1:-status}" in
  status) "$compose" -p autoeval-platform -f compose.json -f compose.workers.json ps ;;
  start) "$compose" -p autoeval-platform -f compose.json -f compose.workers.json up -d ;;
  stop) "$compose" -p autoeval-platform -f compose.json -f compose.workers.json stop ;;
  check)
    docker exec autoeval-platform_api_1 python -c 'import httpx; r=httpx.get("http://127.0.0.1:8000/health/ready",trust_env=False,timeout=20); r.raise_for_status(); print(r.text)'
    ;;
  backup)
    destination="$root/shared/backups/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -m 700 "$destination"
    docker exec autoeval-platform_postgres_1 pg_dump -U autoeval --no-owner --no-privileges -d autoeval > "$destination/database.sql"
    docker exec autoeval-platform_temporal-db_1 pg_dump -U temporal -p 5433 --no-owner --no-privileges -d temporal > "$destination/temporal.sql"
    docker exec autoeval-platform_temporal-db_1 pg_dump -U temporal -p 5433 --no-owner --no-privileges -d temporal_visibility > "$destination/temporal_visibility.sql"
    image=$(docker inspect --format '{{.Image}}' autoeval-platform_api_1)
    docker run --rm --network container:autoeval-platform_network-anchor_1 --user 1008:1008 --env-file "$root/shared/backend.env" -v "$root/backup_objects.py:/backup_objects.py:ro" -v "$destination:/backups" "$image" python /backup_objects.py
    tar -czf "$destination/credentials.tar.gz" -C "$root/shared" secrets tls backend.env postgres.env temporal-db.env temporal.env storage.env htpasswd
    cp image-lock.json compose.json compose.workers.json nginx.conf "$destination/"
    (cd "$destination" && sha256sum database.sql temporal.sql temporal_visibility.sql objects.tar.gz objects.json credentials.tar.gz > SHA256SUMS)
    echo "Backup complete: $destination"
    ;;
  *) echo 'Usage: trialctl.sh {status|start|stop|check|backup}' >&2; exit 2 ;;
esac
