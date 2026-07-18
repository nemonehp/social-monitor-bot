#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/social-monitor/app}"
BACKUP_DIR="${BACKUP_DIR:-/opt/social-monitor/backups}"
EXPECTED_REVISION="0003_unified_health_categories"

cd "$PROJECT_DIR"

if [[ "${CONFIRM_V1_2_UPGRADE:-}" != "YES" ]]; then
  echo "Set CONFIRM_V1_2_UPGRADE=YES to continue." >&2
  exit 2
fi

[[ -f docker-compose.yml ]] || { echo "docker-compose.yml not found" >&2; exit 1; }
[[ -f .env ]] || { echo ".env not found" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
db_backup="$BACKUP_DIR/pre-v1.2.0-$stamp.dump"
env_target="$(readlink -f .env)"
env_backup="$BACKUP_DIR/pre-v1.2.0-$stamp.env"

printf '[1/8] Environment backup and v1.2 runtime settings: %s\n' "$env_backup"
cp -p "$env_target" "$env_backup"
python3 - "$env_target" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
updates = {
    "DELIVERY_CONCURRENCY": "1",
    # Zero means the complete currently-due source batch, without interleaving.
    "DELIVERY_BATCH_SIZE": "0",
    "CREDENTIAL_HEALTH_PROBE_POSTS": "5",
    "MEDIA_DELETE_AFTER_DELIVERY": "true",
    "PROXY_FAILURES_TO_QUARANTINE": "1",
    "PROXY_REMOVE_AFTER_HOURS": "3",
    "PROXY_LOW_RATIO": "0.5",
    "PROXY_LOW_WATERMARK": "1",
    "HEALTH_ALERT_REPEAT_MINUTES": "360",
    "LIMITED_ALERT_THRESHOLD_SECONDS": "1800",
    "DAILY_REPORT_HOUR_MOSCOW": "0",
    "DAILY_REPORT_TOP_SOURCES": "5",
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
    output.append("# v1.2 unified delivery and health policy")
    output.extend(f"{key}={updates[key]}" for key in missing)
path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
PY

printf '[2/8] PostgreSQL backup: %s\n' "$db_backup"
docker compose exec -T postgres \
  sh -lc 'exec pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$db_backup"
test -s "$db_backup"

echo "[3/8] Stop application services"
docker compose stop bot scheduler worker-vk worker-tg delivery

echo "[4/8] Build one shared application image"
docker compose build --pull migrate

echo "[5/8] Apply database migration"
docker compose run --rm migrate

echo "[6/8] Start services"
docker compose up -d --remove-orphans

echo "[7/8] Verify migration and containers"
revision="$(docker compose exec -T postgres sh -lc \
  'psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"')"
if [[ "$revision" != "$EXPECTED_REVISION" ]]; then
  echo "Unexpected Alembic revision: $revision" >&2
  docker compose ps -a
  exit 1
fi

echo "[8/8] Remove only dangling images"
docker image prune -f >/dev/null

docker compose ps -a
printf '\nUpgrade completed. Database backup: %s\n' "$db_backup"
printf 'Environment backup: %s\n' "$env_backup"
printf 'Check logs: docker compose logs --since=10m --tail=300 scheduler worker-tg worker-vk delivery bot\n'
