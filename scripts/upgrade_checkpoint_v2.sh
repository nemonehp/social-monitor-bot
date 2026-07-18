#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/social-monitor/app}"
BACKUP_DIR="${BACKUP_DIR:-/opt/social-monitor/backups}"
EXPECTED_REVISION="0002_checkpoint_monitoring"

if [[ "${CONFIRM_CHECKPOINT_RESET:-}" != "YES" ]]; then
  cat >&2 <<'MSG'
This upgrade deletes old bootstrap items, deliveries and collection jobs.
Sources, users, settings, credentials and proxies are preserved.
Run with: CONFIRM_CHECKPOINT_RESET=YES ./scripts/upgrade_checkpoint_v2.sh
MSG
  exit 2
fi

cd "$PROJECT_DIR"
[[ -f docker-compose.yml ]] || { echo "docker-compose.yml not found" >&2; exit 1; }
[[ -f .env ]] || { echo ".env not found" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$BACKUP_DIR/pre-v1.1.0-$stamp.dump"

echo "[1/7] PostgreSQL backup: $backup"
docker compose exec -T postgres \
  sh -lc 'exec pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$backup"
test -s "$backup"

echo "[2/7] Stop application services"
docker compose stop bot scheduler worker-vk worker-tg delivery

echo "[3/7] Build updated images"
docker compose build --pull

echo "[4/7] Apply database migration"
docker compose run --rm migrate

echo "[5/7] Remove files from the obsolete media bootstrap"
docker compose run --rm --no-deps --entrypoint sh worker-tg \
  -lc 'find /app/data/media -mindepth 1 -depth -delete'

echo "[6/7] Start services"
docker compose up -d --remove-orphans

echo "[7/7] Verify migration and containers"
revision="$(docker compose exec -T postgres sh -lc \
  'psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"')"
if [[ "$revision" != "$EXPECTED_REVISION" ]]; then
  echo "Unexpected Alembic revision: $revision" >&2
  docker compose ps -a
  exit 1
fi

docker compose ps -a
printf '\nUpgrade completed. Backup: %s\n' "$backup"
printf 'Check logs: docker compose logs --since=10m --tail=300 scheduler worker-tg worker-vk delivery bot\n'
