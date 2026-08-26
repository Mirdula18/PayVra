# PAYVRA — developer entrypoints. Requires GNU make.
# DATABASE_URL (from .env / environment) is the only switch between local Docker
# Postgres and Neon. See ADR-007.

.DEFAULT_GOAL := help
.PHONY: help install db-up db-down migrate seed seed-reset seed-demo dev web test lint typecheck fmt verify-razorpay inspect-webhook demo-link verify-llm tunnel

# --- venv layout differs on Windows vs POSIX ---
VENV := .venv
ifeq ($(OS),Windows_NT)
  VENV_BIN := $(VENV)/Scripts
else
  VENV_BIN := $(VENV)/bin
endif
PY      := $(VENV_BIN)/python
PIP     := $(VENV_BIN)/pip
ALEMBIC := $(VENV_BIN)/alembic
UVICORN := $(VENV_BIN)/uvicorn
PYTEST  := $(VENV_BIN)/pytest
RUFF    := $(VENV_BIN)/ruff
MYPY    := $(VENV_BIN)/mypy

# alembic.ini lives under api/; %(here)s makes script_location cwd-independent.
ALEMBIC_INI := api/alembic.ini

help:
	@echo "PAYVRA make targets:"
	@echo "  install     Create .venv, install backend (editable) + web deps"
	@echo "  db-up       Start Docker Postgres and run migrations to head"
	@echo "  db-down     Stop and remove the Docker Postgres container"
	@echo "  migrate     alembic upgrade head (against DATABASE_URL)"
	@echo "  seed        Seed 120 invoices / 34 counterparties / 60 days history"
	@echo "  seed-reset  Truncate all app tables, then reseed"
	@echo "  seed-demo   Deterministic curated state for the pitch"
	@echo "  dev         Run the API (uvicorn, reload). Web: 'make web'"
	@echo "  web         Run the Vite dev server"
	@echo "  test        Run pytest"
	@echo "  lint / typecheck / fmt   ruff check / mypy / ruff format"
	@echo "  verify-razorpay  Probe the LIVE test-mode Razorpay API (creates 2 links)"
	@echo "  inspect-webhook  Show what a REAL signed webhook delivered"
	@echo "  demo-link        Create a REAL payable link on a REAL seeded invoice"
	@echo "  verify-llm       Draft against a REAL model and validate the output"
	@echo "  tunnel           cloudflared tunnel for local webhook delivery"

install:
	python -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	cd web && npm install

db-up:
	docker compose up -d db
	docker compose exec -T db bash -c 'until pg_isready -U payvra -d payvra; do sleep 1; done'
	$(ALEMBIC) -c $(ALEMBIC_INI) upgrade head

db-down:
	docker compose down

migrate:
	$(ALEMBIC) -c $(ALEMBIC_INI) upgrade head

seed:
	$(PY) -m app.seed

seed-reset:
	$(PY) -m app.seed --reset

seed-demo:
	$(PY) -m app.seed --demo

dev:
	$(UVICORN) app.main:app --reload --host 0.0.0.0 --port 8000

web:
	cd web && npm run dev

test:
	$(PYTEST)

lint:
	$(RUFF) check api

typecheck:
	$(MYPY) -p app

fmt:
	$(RUFF) format api

# Talks to the real Razorpay test API. Creates exactly one payment link and cancels it again,
# so a repeated run does not eat into the 30-link test-mode cap. --keep leaves it payable.
verify-razorpay:
	cd api && ../$(PY) -m scripts.verify_razorpay $(ARGS)

# The inbound half. Run AFTER paying a link: reports what a genuinely Razorpay-signed
# delivery carried, and whether webhooks.extract reads a real envelope. ARGS="--raw" dumps
# the full payload (counterparty PII -- do not paste it publicly).
inspect-webhook:
	cd api && ../$(PY) -m scripts.inspect_webhook $(ARGS)

# Creates a REAL payment link on a REAL seeded invoice and leaves it payable, so the
# reconciliation loop can be proven before the Phase 6 agent exists to create links itself.
# ARGS="--invoice INV-2026-1020" picks one; default is the highest-priority unpaid.
demo-link:
	cd api && ../$(PY) -m scripts.create_demo_link $(ARGS)

# The drafting half. Proves a REAL model returns parseable, validating JSON -- the thing every
# monkeypatched test cannot. Needs GROQ_API_KEY or GEMINI_API_KEY and LLM_ENABLED=true.
# ARGS="--show" prints each drafted message in full.
verify-llm:
	cd api && ../$(PY) -m scripts.verify_llm $(ARGS)

# Public HTTPS URL for webhook delivery. Register the printed URL (plus
# /api/v1/webhooks/razorpay) in the Razorpay Dashboard under Settings -> Webhooks, TEST mode.
tunnel:
	cloudflared tunnel --url http://localhost:8000
