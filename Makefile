.PHONY: up down logs test lint migrate key

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

key:
	python scripts/generate_key.py
