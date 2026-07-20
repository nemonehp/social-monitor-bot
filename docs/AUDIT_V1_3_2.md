# Аудит hotfix v1.3.2

## Причина инцидента

`InputMediaPhoto`, `InputMediaVideo` и `InputMediaDocument` в aiogram 3 являются замороженными Pydantic-моделями. Код v1.3.1 создавал media group, а затем пытался изменить `media_group[0].caption` и `parse_mode`. Это приводило к `frozen_instance`, возврату доставки в retry и повтору примерно каждые 30 секунд.

## Исправление

- Подпись и `ParseMode.HTML` передаются конструктору первого media-элемента.
- После создания media-объекты больше не мутируются.
- Неожиданные исключения остаются retryable, чтобы не терять публикации, но получают ограниченный экспоненциальный backoff до 15 минут.
- Миграция базы не требуется; застрявшая доставка отправится после запуска исправленного worker.

## Проверки

- Ruff.
- mypy для 53 исходных файлов.
- 67 тестов, включая регрессионный тест frozen media group.
- Python compileall.
- Bash syntax.
- Docker Compose YAML.
- Применение production-патча к чистой v1.3.1.
