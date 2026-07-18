# Обновление до v1.1.0

Версия v1.1.0 меняет модель первичного обхода и намеренно сбрасывает результаты
исторического bootstrap версии 1.0.

## Что сохраняется

- источники и их регионы;
- allowlist пользователей;
- настройки бота и сигнального чата;
- Telegram StringSession;
- VK-токены;
- прокси и их состояния;
- ключ шифрования в `.env`.

## Что удаляется один раз миграцией 0002

- старые `items`, загруженные историческим bootstrap;
- связанные `deliveries` и строки `media`;
- текущая очередь `collection_jobs`.

Время применения миграции становится `monitor_from_at` и `checkpoint_at` для
всех существующих источников. Всё, что опубликовано раньше этой отметки, не
считается новым.

## Безопасный порядок

```bash
cd /opt/social-monitor/app
mkdir -p /opt/social-monitor/backups

docker compose exec -T postgres \
  sh -lc 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > "/opt/social-monitor/backups/pre-v1.1.0-$(date -u +%Y%m%dT%H%M%SZ).dump"

docker compose stop bot scheduler worker-vk worker-tg delivery

docker compose build --pull
docker compose run --rm migrate

docker compose run --rm --no-deps --entrypoint sh worker-tg \
  -lc 'find /app/data/media -mindepth 1 -depth -delete'

docker compose up -d --remove-orphans
docker compose ps -a
```

Тот же порядок автоматизирован скриптом:

```bash
CONFIRM_CHECKPOINT_RESET=YES ./scripts/upgrade_checkpoint_v2.sh
```

## Проверка

```bash
docker compose exec -T postgres sh -lc \
  'psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"'

docker compose logs --since=10m --tail=300 scheduler worker-tg worker-vk delivery bot
```

Ожидаемая версия миграции: `0002_checkpoint_monitoring`.
