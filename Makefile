.PHONY: up down migrate test lint fmt

up:
	docker compose up -d postgres redis ollama

down:
	docker compose down

migrate:
	python scripts/apply_migrations.py

test:
	pytest -q

lint:
	ruff check src tests

fmt:
	ruff format
