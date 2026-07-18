#!/usr/bin/env bash
set -u
set -o pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/social-monitor/app}"
if [[ ! -f "$PROJECT_DIR/docker-compose.yml" ]]; then
  echo "ERROR: docker-compose.yml not found in $PROJECT_DIR" >&2
  exit 1
fi
cd "$PROJECT_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/social-monitor-diagnostics-${STAMP}.txt"

sanitize() {
  sed -E \
    -e 's#([A-Za-z][A-Za-z0-9+.-]*://)[^/@:[:space:]]+:[^/@[:space:]]+@#\1<redacted>@#g' \
    -e 's/[0-9]{6,}:[A-Za-z0-9_-]{20,}/<BOT_TOKEN>/g'
}

exec > >(sanitize | tee "$OUT") 2>&1

section() {
  printf '\n\n================================================================================\n'
  printf '%s\n' "$1"
  printf '================================================================================\n'
}

try() {
  printf '\n$'
  printf ' %q' "$@"
  printf '\n'
  "$@" || printf '[command failed, exit=%s]\n' "$?"
}

psql_db() {
  docker compose exec -T postgres sh -lc \
    'exec psql -X -P pager=off -P border=2 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

section "1. HOST / DOCKER"
try date -u --iso-8601=seconds
try hostnamectl
try uptime
try free -h
try df -h /
try docker --version
try docker compose version
try docker info --format 'Server={{.ServerVersion}} Driver={{.Driver}} CPUs={{.NCPU}} Memory={{.MemTotal}}'

section "2. SAFE APPLICATION SETTINGS"
if [[ -f .env ]]; then
  grep -E '^(DEFAULT_POLL_INTERVAL_SECONDS|MIN_POLL_INTERVAL_SECONDS|SCHEDULER_TICK_SECONDS|JOB_LEASE_SECONDS|MAX_JOB_ATTEMPTS|MAX_CREDENTIAL_TRIES_PER_SOURCE|VK_WORKER_CONCURRENCY|TG_MAX_ACTIVE_ACCOUNTS|DELIVERY_CONCURRENCY|TG_REQUIRE_NON_RU|IP_CHECK_URL|LOG_LEVEL)=' .env || true
else
  echo '.env not found'
fi

section "3. COMPOSE STATE"
try docker compose ps -a
for service in postgres migrate bot scheduler worker-vk worker-tg delivery; do
  cid="$(docker compose ps -q "$service" 2>/dev/null || true)"
  if [[ -n "$cid" ]]; then
    echo "--- $service ---"
    docker inspect --format \
      'name={{.Name}} status={{.State.Status}} running={{.State.Running}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}} restarts={{.RestartCount}} started={{.State.StartedAt}} finished={{.State.FinishedAt}} exit={{.State.ExitCode}} error={{.State.Error}}' \
      "$cid" || true
  else
    echo "--- $service: container not found ---"
  fi
done
try docker compose top scheduler
try docker compose top worker-tg
try docker compose top worker-vk

section "4. DATABASE / QUEUES / SOURCES / ACCOUNTS"
psql_db <<'SQL'
\pset null '∅'
\timing on

\echo '--- database clock and migration ---'
SELECT now() AS db_now, current_database() AS database, current_user AS db_user;
SELECT * FROM alembic_version;

\echo '--- runtime app settings (signal chat id hidden) ---'
SELECT key,
       CASE WHEN key = 'signal_chat_id' THEN '<configured>' ELSE value::text END AS value
FROM app_settings
ORDER BY key;

\echo '--- sources by platform/status ---'
SELECT lower(platform::text) AS platform,
       lower(status::text) AS status,
       count(*) AS sources,
       count(*) FILTER (WHERE next_check_at <= now()) AS due_now,
       min(next_check_at) AS oldest_next_check,
       max(last_check_at) AS latest_check,
       max(last_success_at) AS latest_success
FROM sources
GROUP BY platform, status
ORDER BY platform, status;

\echo '--- source progress by platform ---'
SELECT lower(s.platform::text) AS platform,
       count(*) FILTER (WHERE lower(s.status::text) = 'active') AS active,
       count(*) FILTER (WHERE s.last_check_at IS NULL AND lower(s.status::text) = 'active') AS never_checked,
       count(*) FILTER (WHERE s.last_success_at IS NULL AND lower(s.status::text) = 'active') AS never_succeeded,
       count(*) FILTER (WHERE st.bootstrap_completed IS TRUE) AS bootstrap_done,
       count(*) FILTER (WHERE st.bootstrap_completed IS FALSE) AS bootstrap_pending,
       count(*) FILTER (WHERE s.consecutive_failures > 0) AS with_failures
FROM sources s
LEFT JOIN source_states st ON st.source_id = s.id
GROUP BY s.platform
ORDER BY s.platform;

\echo '--- collection jobs by platform/status ---'
SELECT lower(platform::text) AS platform,
       lower(status::text) AS status,
       count(*) AS jobs,
       count(*) FILTER (WHERE run_after <= now()) AS runnable_now,
       min(run_after) AS oldest_run_after,
       max(attempts) AS max_attempts
FROM collection_jobs
GROUP BY platform, status
ORDER BY platform, status;

\echo '--- active queue totals ---'
SELECT lower(platform::text) AS platform,
       count(*) AS pending_or_retry,
       count(*) FILTER (WHERE run_after <= now()) AS runnable_now,
       count(*) FILTER (WHERE locked_until IS NOT NULL AND locked_until < now()) AS expired_locks
FROM collection_jobs
WHERE lower(status::text) IN ('pending', 'retry', 'running')
GROUP BY platform
ORDER BY platform;

\echo '--- oldest active jobs (up to 60) ---'
SELECT j.id,
       lower(j.platform::text) AS platform,
       lower(j.status::text) AS status,
       j.source_id,
       left(replace(coalesce(s.title, ''), E'\n', ' '), 40) AS title,
       left(s.normalized_link, 80) AS link,
       j.attempts,
       j.run_after,
       j.locked_until,
       nullif(j.worker_id, '') AS worker_id,
       left(replace(coalesce(j.last_error, ''), E'\n', ' '), 300) AS last_error
FROM collection_jobs j
JOIN sources s ON s.id = j.source_id
WHERE lower(j.status::text) IN ('pending', 'retry', 'running')
ORDER BY j.run_after, j.id
LIMIT 60;

\echo '--- duplicate active jobs for one source (should be empty) ---'
SELECT source_id, count(*) AS active_jobs
FROM collection_jobs
WHERE lower(status::text) IN ('pending', 'retry', 'running')
GROUP BY source_id
HAVING count(*) > 1
ORDER BY active_jobs DESC, source_id
LIMIT 50;

\echo '--- credentials (secrets/config hidden) ---'
SELECT id,
       lower(platform::text) AS platform,
       CASE WHEN length(label) > 6 THEN left(label, 3) || '…' || right(label, 2) ELSE '***' END AS label_masked,
       lower(status::text) AS status,
       cooldown_until,
       last_success_at,
       last_health_check_at,
       last_health_ok_at,
       health_failures,
       dead_since,
       dead_notified_at,
       requests_count,
       left(replace(coalesce(last_error, ''), E'\n', ' '), 300) AS last_error
FROM credentials
ORDER BY platform, status, id;

\echo '--- proxy pool (URLs hidden) ---'
SELECT id,
       display,
       country_code,
       external_ip,
       latency_ms,
       lower(status::text) AS status,
       failures,
       successes,
       last_check_at,
       last_success_at,
       quarantine_until,
       left(replace(coalesce(last_error, ''), E'\n', ' '), 300) AS last_error
FROM proxies
ORDER BY status, id;

\echo '--- oldest active sources that are due ---'
SELECT id,
       lower(platform::text) AS platform,
       left(replace(coalesce(title, ''), E'\n', ' '), 45) AS title,
       left(normalized_link, 90) AS link,
       category,
       subcategory,
       region,
       federal_district,
       next_check_at,
       last_check_at,
       last_success_at,
       consecutive_failures,
       last_error_code,
       left(replace(coalesce(last_error_text, ''), E'\n', ' '), 250) AS last_error
FROM sources
WHERE lower(status::text) = 'active' AND next_check_at <= now()
ORDER BY next_check_at, id
LIMIT 60;

\echo '--- recent source errors ---'
SELECT id,
       lower(platform::text) AS platform,
       left(replace(coalesce(title, ''), E'\n', ' '), 45) AS title,
       consecutive_failures,
       last_check_at,
       last_error_code,
       left(replace(coalesce(last_error_text, ''), E'\n', ' '), 300) AS last_error
FROM sources
WHERE consecutive_failures > 0 OR last_error_code <> '' OR last_error_text <> ''
ORDER BY last_check_at DESC NULLS LAST, id DESC
LIMIT 60;

\echo '--- items/deliveries ---'
SELECT lower(platform::text) AS platform,
       lower(item_type::text) AS item_type,
       count(*) AS items,
       max(created_at) AS latest_created,
       max(published_at) AS latest_published
FROM items
GROUP BY platform, item_type
ORDER BY platform, item_type;

SELECT lower(status::text) AS status,
       count(*) AS deliveries,
       count(*) FILTER (WHERE run_after <= now()) AS runnable_now,
       min(run_after) AS oldest_run_after,
       max(attempts) AS max_attempts
FROM deliveries
GROUP BY status
ORDER BY status;

\echo '--- PostgreSQL sessions/waits ---'
SELECT pid,
       usename,
       application_name,
       state,
       wait_event_type,
       wait_event,
       now() - query_start AS query_age,
       left(replace(query, E'\n', ' '), 180) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid()
ORDER BY query_start NULLS LAST;
SQL

section "5. TELEGRAM WORKER NETWORK PROBE (NO CREDENTIALS USED)"
docker compose exec -T worker-tg python - <<'PY' || true
import json
import os
import socket
import ssl
import urllib.request

print('python network probe')
url = os.getenv('IP_CHECK_URL', 'https://ipwho.is/')
try:
    req = urllib.request.Request(url, headers={'User-Agent': 'social-monitor-diagnostics/1.0'})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read(200_000)
    data = json.loads(raw)
    print('ip_check:', {
        'success': data.get('success'),
        'ip': data.get('ip'),
        'country_code': data.get('country_code'),
        'message': data.get('message'),
    })
except Exception as exc:
    print('ip_check_error:', type(exc).__name__, str(exc))

for host in ('telegram.org', 'api.telegram.org'):
    try:
        addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        print('dns', host, addresses[:10])
    except Exception as exc:
        print('dns_error', host, type(exc).__name__, str(exc))

for host in ('telegram.org', 'api.telegram.org', '149.154.167.51', '149.154.167.91'):
    try:
        with socket.create_connection((host, 443), timeout=8):
            print('tcp_443_ok', host)
    except Exception as exc:
        print('tcp_443_error', host, type(exc).__name__, str(exc))
PY

section "6. SCHEDULER LOGS (LAST 60 MINUTES, TAIL 350)"
docker compose logs --since=60m --no-color scheduler 2>&1 | tail -n 350 || true

section "7. TELEGRAM WORKER LOGS (LAST 60 MINUTES, TAIL 700)"
docker compose logs --since=60m --no-color worker-tg 2>&1 | tail -n 700 || true

section "8. VK WORKER LOGS (LAST 60 MINUTES, TAIL 350)"
docker compose logs --since=60m --no-color worker-vk 2>&1 | tail -n 350 || true

section "9. BOT / DELIVERY LOGS (LAST 30 MINUTES, TAIL 250 EACH)"
docker compose logs --since=30m --no-color bot 2>&1 | tail -n 250 || true
docker compose logs --since=30m --no-color delivery 2>&1 | tail -n 250 || true

section "10. FINAL SNAPSHOT"
try docker compose ps -a
printf '\nDiagnostic file: %s\n' "$OUT"
printf 'Upload this TXT file. Bot tokens, URL passwords, encrypted sessions and encryption keys are not queried.\n'
