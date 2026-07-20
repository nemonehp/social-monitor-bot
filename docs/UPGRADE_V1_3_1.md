# Обновление до v1.3.1

Hotfix не меняет схему базы данных. Alembic revision остаётся `0004_capacity_forum_integrity`.

## Исправления

- Служебные сообщения Telegram при создании тем больше не получают ответ `Доступ к боту не разрешён`.
- Выбранный администратором интервал сохраняется всегда.
- Для VK и Telegram рассчитывается отдельный фактический интервал.
- Если мощности одной платформы недостаточно, другая продолжает работать с выбранной частотой.
- При частичном дефиците платформа замедляется до ближайшего безопасного интервала.
- При нуле рабочих аккаунтов или IP ставится на паузу только эта платформа.
- Дублирующие предупреждения об отсутствии токенов/сессий отключены; capacity-alert повторяется не чаще раза в 6 часов.
- Сообщение о ёмкости отдельно показывает, достаточно ли IP и сколько именно аккаунтов требуется.

## Установка

```bash
cd /opt/social-monitor/app
git status --short
PATCH=/opt/social-monitor-bot-v1.3.1-live-server.patch
git apply --check "$PATCH"
git apply "$PATCH"
git add .
git commit -m "Fix platform cadence and forum binding alerts"
CONFIRM_V1_3_1_UPGRADE=YES ./scripts/upgrade_v1_3_1.sh
```

## Проверка

```bash
docker compose ps -a
docker compose logs --since=10m --no-color scheduler bot \
  | grep -iE 'traceback|exception|unexpected|task_failed' \
  || echo "Падений нет"
```

В разделе частоты и в состоянии системы должны отображаться запрошенный интервал и отдельный фактический режим VK/TG.
