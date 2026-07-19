.PHONY: up down logs test lint quality migrate key

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

migrate:
	docker compose run --rm migrate alembic upgrade head

test:
	pytest -q

lint:
	ruff check app tests

quality:
	./scripts/quality_check.sh

key:
	python scripts/generate_key.py
