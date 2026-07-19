#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/social-monitor/app}"
BACKUP_DIR="${BACKUP_DIR:-/opt/social-monitor/backups}"
EXPECTED_REVISION="0003_unified_health_categories"

if [[ "${CONFIRM_V1_2_2_UPGRADE:-}" != "YES" ]]; then
  echo "Set CONFIRM_V1_2_2_UPGRADE=YES to continue." >&2
  exit 2
fi

cd "$PROJECT_DIR"
[[ -f docker-compose.yml ]] || { echo "docker-compose.yml not found" >&2; exit 1; }
[[ -f .env ]] || { echo ".env not found" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
db_backup="$BACKUP_DIR/pre-v1.2.2-$stamp.dump"
env_target="$(readlink -f .env)"
env_backup="$BACKUP_DIR/pre-v1.2.2-$stamp.env"

printf '[1/7] Environment backup: %s\n' "$env_backup"
cp -p "$env_target" "$env_backup"

printf '[2/7] PostgreSQL backup: %s\n' "$db_backup"
docker compose exec -T postgres \
  sh -lc 'exec pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$db_backup"
test -s "$db_backup"

echo "[3/7] Stop application services"
docker compose stop bot scheduler worker-vk worker-tg delivery || true

echo "[4/7] Build shared application image"
docker compose build --pull migrate

echo "[5/7] Apply pending migrations and start services"
docker compose run --rm migrate
docker compose up -d --remove-orphans

echo "[6/7] Verify migration"
revision="$(docker compose exec -T postgres sh -lc \
  'psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"')"
if [[ "$revision" != "$EXPECTED_REVISION" ]]; then
  echo "Unexpected Alembic revision: $revision" >&2
  docker compose ps -a
  exit 1
fi

echo "[7/7] Remove dangling images and show state"
docker image prune -f >/dev/null
docker compose ps -a
printf '\nUpgrade completed. Database backup: %s\n' "$db_backup"
printf 'Environment backup: %s\n' "$env_backup"
printf 'Check logs: docker compose logs --since=10m --tail=300 scheduler worker-tg worker-vk delivery bot\n'
