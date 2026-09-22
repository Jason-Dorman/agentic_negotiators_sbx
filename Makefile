# Convenience wrappers around the three toolchains. Every target here is exactly what CI runs,
# so a green `make ci` locally means a green pipeline. See docs/test_strategy.md section 10.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# The toolchain installs to per-user directories that a non-login shell does not pick up.
# Prepending them here means `make` behaves the same from a terminal, an IDE task runner or
# CI. Anything already on PATH still wins. The same resolution, for the pre-commit hooks,
# lives in infra/scripts/toolchain.sh.
export PATH := $(HOME)/.local/bin:$(HOME)/.foundry/bin:$(PATH)

.PHONY: help setup lint format test ci up down logs reset-db hooks

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install every toolchain and the pre-commit hooks
	uv sync --all-groups
	pnpm install
	forge --version
	uv run pre-commit install

hooks: ## Run every pre-commit hook over the whole tree
	uv run pre-commit run --all-files

lint: ## Format check, lint and type check, all three languages
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy .
	pnpm run format:check
	pnpm run lint
	pnpm run typecheck
	forge fmt --check --root contracts
	uv run python infra/scripts/check_spdx.py
	uv run python infra/scripts/secret_scan.py

format: ## Apply formatters
	uv run ruff format .
	uv run ruff check --fix .
	pnpm exec prettier --write .
	forge fmt --root contracts

test: ## Run every test suite
	uv run pytest
	pnpm run test
	forge test --root contracts

ci: lint test ## What CI runs

up: ## Start PostgreSQL and Anvil (local profile)
	docker compose --profile local up -d --wait

down: ## Stop the local profile, keeping data
	docker compose --profile local down

reset-db: ## Stop the local profile and discard the database volume
	docker compose --profile local down -v

logs: ## Follow the local profile logs
	docker compose --profile local logs -f
