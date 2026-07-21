.PHONY: up app down migrate preflight test lint fmt deploy

up:
	docker compose up -d postgres redis ollama

app:
	docker compose --profile ui up -d

down:
	docker compose down

migrate:
	python scripts/apply_migrations.py

preflight:
	./scripts/with_paddle_runtime.sh python scripts/preflight.py

test:
	pytest -q

lint:
	ruff check src tests

fmt:
	ruff format

deploy:
	./scripts/deploy.sh
