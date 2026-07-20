#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT_DIR="${PROJECT_DIR:-/opt/social-monitor/app}"
BACKUP_DIR="${BACKUP_DIR:-/opt/social-monitor/backups}"
EXPECTED_REVISION="0004_capacity_forum_integrity"

if [[ "${CONFIRM_V1_3_2_UPGRADE:-}" != "YES" ]]; then
  echo "Set CONFIRM_V1_3_2_UPGRADE=YES to continue." >&2
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
db_backup="$BACKUP_DIR/pre-v1.3.2-$stamp.dump"
env_target="$(readlink -f .env)"
env_backup="$BACKUP_DIR/pre-v1.3.2-$stamp.env"

printf '[1/8] Environment backup: %s\n' "$env_backup"
cp -p "$env_target" "$env_backup"
chmod 600 "$env_backup"
python3 - "$env_target" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
updates = {
    "ACCOUNT_DAILY_BUDGET_FRACTION": "0.30",
    "VK_OPERATIONAL_DAILY_REQUEST_BUDGET": "100000",
    "TG_OPERATIONAL_DAILY_REQUEST_BUDGET": "250000",
    "VK_ESTIMATED_REQUESTS_PER_SOURCE_CYCLE": "2.25",
    "TG_ESTIMATED_REQUESTS_PER_SOURCE_CYCLE": "2.0",
    "VK_MAX_ACCOUNTS_PER_IP": "3",
    "CAPACITY_GUARD_ENABLED": "true",
    "CAPACITY_ALERT_REPEAT_MINUTES": "360",
    "CAPACITY_MAX_EFFECTIVE_INTERVAL_SECONDS": "86400",
    "INTEGRITY_GAP_RETRY_SECONDS": "15",
    "INTEGRITY_GAP_ALERT_AFTER": "3",
    "TOKEN_RATE_LIMIT_PENALTY_MINUTES": "60",
    "VK_ASSIGNMENT_EPOCH_MINUTES": "60",
    "VIDEO_PREVIEW_OVERLAY": "true",
    "DELIVERY_CONCURRENCY": "1",
}
lines = path.read_text(encoding="utf-8").splitlines()
seen: set[str] = set()
output: list[str] = []
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        output.append(line)
        continue
    key = line.split("=", 1)[0].strip()
    if key in updates:
        output.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        output.append(line)
missing = [key for key in updates if key not in seen]
if missing:
    if output and output[-1].strip():
        output.append("")
    output.append("# v1.3.2 immutable media-group hotfix")
    output.extend(f"{key}={updates[key]}" for key in missing)
path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
PY

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
printf 'Queued deliveries will retry automatically with the immutable media-group fix.\n'
printf 'Check logs: docker compose logs --since=10m --tail=400 scheduler worker-tg worker-vk delivery bot\n'
