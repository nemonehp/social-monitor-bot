# Обновление до v1.2.2

Версия не добавляет новую миграцию базы. Она применяет существующий Alembic head
`0003_unified_health_categories`, обновляет приложение и перезапускает сервисы.

## Изменения

- заголовки `🔵 TG · ПОСТ/ИСТОРИЯ` и `🟢 VK · ПОСТ/ИСТОРИЯ`;
- постоянная нижняя кнопка `Главное меню` в личном диалоге;
- кнопка очищает любое FSM-состояние и временные файлы импорта;
- безопасная обработка устаревших callback-сообщений и отсутствующих документов;
- неблокирующая запись Telegram-превью и очистка медиа;
- более строгая проверка VK endpoint через прокси;
- воспроизводимый quality-check: Ruff, mypy, pytest, compileall и shell syntax.

## Production

```bash
cd /opt/social-monitor/app
CONFIRM_V1_2_2_UPGRADE=YES ./scripts/upgrade_v1_2_2.sh
```

После обновления:

```bash
docker compose ps -a
docker compose exec -T postgres sh -lc \
  'psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"'
docker compose logs --since=10m --tail=300 scheduler worker-tg worker-vk delivery bot
```

Ожидаемая ревизия: `0003_unified_health_categories`.
