# Convenience wrappers around the three toolchains. Every target here is exactly what CI runs,
# so a green `make ci` locally means a green pipeline. See docs/test_strategy.md section 10.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# The toolchain installs to per-user directories that a non-login shell does not pick up.
# Prepending them here means `make` behaves the same from a terminal, an IDE task runner or
# CI. Anything already on PATH still wins. The same resolution, for the pre-commit hooks,
# lives in infra/scripts/toolchain.sh.
export PATH := $(HOME)/.local/bin:$(HOME)/.foundry/bin:$(PATH)

.PHONY: help setup lint format test gates ci up down logs reset-db hooks abi fixtures artefacts snapshot snapshot-check coverage-contracts

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install every toolchain and the pre-commit hooks
	# --all-extras installs negotiation-protocol[tools], which is web3. The extra exists so the
	# agent service image does not get an RPC client (ADR-035); a developer machine runs the
	# reconstruction tool and type-checks it, so it needs one.
	uv sync --all-groups --all-extras
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
	$(MAKE) artefacts

format: ## Apply formatters
	uv run ruff format .
	uv run ruff check --fix .
	pnpm exec prettier --write .
	forge fmt --root contracts

test: ## Run every test suite
	uv run pytest
	pnpm run test
	forge test --root contracts

# --------------------------------------------------------------------------------------
# Generated protocol artefacts. Both generators are deterministic, so regenerate-and-diff is
# the whole check; `artefacts` is what CI runs and `abi`/`fixtures` are what fixes a failure.
# --------------------------------------------------------------------------------------

abi: ## Re-export the contract ABIs into packages/protocol/abi/
	forge build --root contracts
	uv run python packages/protocol/tools/export_abi.py

fixtures: ## Regenerate the EIP-712 fixture (a change here is a protocol version bump)
	uv run python packages/protocol/tools/generate_eip712_fixtures.py

artefacts: ## Check the committed ABIs and fixture match their generators
	forge build --root contracts
	uv run python packages/protocol/tools/export_abi.py --check
	uv run python packages/protocol/tools/generate_eip712_fixtures.py
	git diff --exit-code -- packages/protocol/fixtures/eip712.v1.json

# The snapshot path is given explicitly in both targets. `--root` says where the project is, but
# the snapshot path resolves against the working directory: the bare form wrote a stray
# `.gas-snapshot` at the repository root and made the CI check read that non-existent file instead
# of the committed one.

snapshot: ## Rewrite the committed gas snapshot (unit tests only; fuzz gas moves with the seed)
	forge snapshot --snap contracts/.gas-snapshot --no-match-path "test/invariant/*" --root contracts
	# `forge snapshot` writes the file with no trailing newline, which `.editorconfig` requires and
	# the `end-of-file-fixer` pre-commit hook supplies -- by modifying the file and failing the
	# commit, after it has already been staged. Adding it here means the committed file is correct
	# the first time. `forge snapshot --check` is indifferent to it, which is checked in CI.
	@[ -n "$$(tail -c 1 contracts/.gas-snapshot)" ] && printf '\n' >> contracts/.gas-snapshot || true

snapshot-check: ## Fail on a gas regression over 10 percent (test_strategy 4.3)
	forge snapshot --check contracts/.gas-snapshot --tolerance 10 \
		--no-match-path "test/invariant/*" --root contracts

coverage-contracts: ## The 100 percent gate on NegotiationExchange (test_strategy 10)
	forge coverage --root contracts --no-match-coverage "(test|script)/" --report summary \
		> /tmp/negotiation-coverage.txt
	@cat /tmp/negotiation-coverage.txt
	@uv run python infra/scripts/check_contract_coverage.py /tmp/negotiation-coverage.txt

gates: snapshot-check coverage-contracts ## The two gates that are neither lint nor test

ci: lint test gates ## What CI runs

up: ## Start PostgreSQL and Anvil (local profile)
	docker compose --profile local up -d --wait

down: ## Stop the local profile, keeping data
	docker compose --profile local down

reset-db: ## Stop the local profile and discard the database volume
	docker compose --profile local down -v

logs: ## Follow the local profile logs
	docker compose --profile local logs -f
