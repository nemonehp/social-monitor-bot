#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT_DIR="${PROJECT_DIR:-/opt/social-monitor/app}"
BACKUP_DIR="${BACKUP_DIR:-/opt/social-monitor/backups}"
EXPECTED_REVISION="0006_integrity_counter_guard"

if [[ "${CONFIRM_V1_3_4_UPGRADE:-}" != "YES" ]]; then
  echo "Set CONFIRM_V1_3_4_UPGRADE=YES to continue." >&2
  exit 2
fi

cd "$PROJECT_DIR"
[[ -f docker-compose.yml ]] || { echo "docker-compose.yml not found" >&2; exit 1; }
[[ -f .env ]] || { echo ".env not found" >&2; exit 1; }
docker compose config --quiet

available_kb="$(df -Pk "$PROJECT_DIR" | awk 'NR == 2 {print $4}')"
minimum_kb=$((2 * 1024 * 1024))
if [[ -z "$available_kb" || "$available_kb" -lt "$minimum_kb" ]]; then
  echo "At least 2 GiB of free disk space is required before rebuilding images." >&2
  df -h "$PROJECT_DIR" >&2
  exit 1
fi

services_stopped=0
on_error() {
  status=$?
  echo "Upgrade failed with exit code $status." >&2
  if [[ "$services_stopped" == "1" ]]; then
    echo "Attempting to start application services after the failed upgrade..." >&2
    docker compose up -d --remove-orphans || true
    docker compose ps -a || true
  fi
  exit "$status"
}
trap on_error ERR

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
db_backup="$BACKUP_DIR/pre-v1.3.4-$stamp.dump"
env_target="$(readlink -f .env)"
env_backup="$BACKUP_DIR/pre-v1.3.4-$stamp.env"

printf '[1/8] Environment backup: %s\n' "$env_backup"
cp -p "$env_target" "$env_backup"
chmod 600 "$env_backup"

printf '[2/8] PostgreSQL backup: %s\n' "$db_backup"
docker compose up -d postgres
docker compose exec -T postgres \
  sh -lc 'exec pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$db_backup"
test -s "$db_backup"
chmod 600 "$db_backup"

echo "[3/8] Stop application services"
docker compose stop bot scheduler worker-vk worker-tg delivery || true
services_stopped=1

echo "[4/8] Build shared application image"
docker compose build --pull migrate

echo "[5/8] Apply database migration"
docker compose run --rm migrate

echo "[6/8] Start services"
docker compose up -d --remove-orphans
services_stopped=0

echo "[7/8] Verify migration"
revision="$(docker compose exec -T postgres sh -lc \
  'psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"')"
if [[ "$revision" != "$EXPECTED_REVISION" ]]; then
  echo "Unexpected Alembic revision: $revision" >&2
  docker compose ps -a
  exit 1
fi

echo "[8/8] Remove dangling images and show state"
docker image prune -f >/dev/null
docker compose ps -a
printf '\nUpgrade completed. Database backup: %s\n' "$db_backup"
printf 'Environment backup: %s\n' "$env_backup"
printf 'Integrity gap counters are safe before ORM flush.\n'
printf 'Check logs: docker compose logs --since=10m --tail=400 scheduler worker-tg worker-vk delivery bot\n'
