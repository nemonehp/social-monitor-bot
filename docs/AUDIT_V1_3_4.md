# Аудит v1.3.4

## Production-инцидент

`assess_collection_integrity()` падал на `row.consecutive_gaps += 1`, когда строка создавалась в текущей ORM-сессии и insert-default ещё не был применён.

## Риск данных

Исключение возникало внутри транзакции до commit. Задание переводилось в retry, а checkpoint не продвигался. После hotfix отложенные задания продолжаются автоматически.

## Защита

1. Явная инициализация Python-объекта.
2. Нормализующий increment helper.
3. Repair-миграция для возможного schema/data drift.
4. Регрессионные тесты на pre-flush поведение SQLAlchemy.
