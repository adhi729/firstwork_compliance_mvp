.PHONY: init up up-async down logs migrate shell psql test lint reset benchmark

init:            ## copy .env.example to .env if missing
	cp -n .env.example .env || true

up: init         ## build and start db, redis, temporal, migrate, api
	docker compose up -d --build

up-async: init   ## also start Temporal worker and outbox dispatcher
	docker compose --profile async up -d --build

down:
	docker compose --profile async down

logs:
	docker compose logs -f api

migrate:
	docker compose run --rm migrate

shell:
	docker compose exec api python manage.py shell

psql:            ## connect as the owning role (bypasses RLS; use app_user to test RLS)
	docker compose exec db psql -U migrator compliance

test:            ## tests run as migrator so Django can create the test database
	docker compose run --rm -e DATABASE_URL=postgresql://migrator:migrator@db:5432/compliance api pytest

lint:
	docker compose run --rm api sh -c "ruff check . && mypy ."

benchmark: init  ## run the 100k-rule benchmark including Temporal bulk evaluation
	docker compose --profile async up -d --build api worker
	docker compose exec -T api python manage.py run_temporal_benchmark

reset:           ## wipe all volumes (destructive)
	docker compose --profile async down -v
