# CLAUDE.md

Project: Two-Agent Negotiation and Settlement Sandbox. Two AI agents negotiate a price for a fixed quantity of a mock asset, record public actions on an EVM testnet, and settle atomically with test tokens.

## Start here

1. `docs/README.md` is the index. The system spec in `docs/` is the source of truth; the governance documents derive from it.
2. `docs/contributing.md` holds the code standards, import boundaries, and the review checklist. Follow it for every change.
3. `docs/decision_log.md` records decisions and assumptions. Before changing protocol, API, data model, or a requirement, add or update an ADR.
4. `docs/open_questions.md` lists assumptions awaiting the owner's answer. Add to it rather than deciding silently.

## Working with the product owner

- The user is the product owner (PO). If a question or gap arises that the spec or the docs in `docs/` do not settle, ask the PO. Do not assume, do not pick a default silently, and do not bury the choice in code or a decision-log entry marked "assumption". Record the answer once given.
- Docs are updated in lockstep with decisions and code changes, in the same change set. A PR that alters protocol, API, schema, behaviour, or a requirement updates `docs/protocol.md`, `docs/api_contract.md`, `docs/data_model.md`, `docs/prd.md`, and `docs/decision_log.md` as applicable. Docs that lag the code are a defect.

## Non-negotiables

- Never write code that chooses, clamps, or fixes a model-proposed price. The deterministic policy clamps itself only.
- Never construct a signed message from model-supplied bytes. The signer builds it from validated state.
- Never let one agent's mandate, validation feedback, prompt, explanation, or credentials reach the other agent, logs, SSE, or the default export. The observation allowlist in `docs/protocol.md` section 12 is exhaustive.
- Never treat a database row as proof of settlement. Canonical chain events at the confirmation threshold are the only source of economic outcome.
- Never hardcode an agreement to make a live demo pass. A model that cannot settle is a result to report.
- Never log or commit secrets. Keys are references (`env:`, `keystore:`), not values.
- All token amounts are integers in minor units, as strings in JSON, `NUMERIC(78,0)` in PostgreSQL.
- All diagrams in documents are Mermaid.

## Toolchain (see docs/architecture.md section 10)

Python 3.12 with `uv`, `ruff`, `mypy --strict`, `pytest`. Node LTS with `pnpm`, Vitest, Playwright. Foundry for contracts. PostgreSQL 16. Docker Compose profiles `local` and `sepolia`.

## Model integration

Anthropic Claude via the official `anthropic` Python SDK, default `claude-opus-5`, structured outputs, adaptive thinking, SDK retries off, one explicit repair. See `docs/architecture.md` section 7 and ADR-014, ADR-015.

## When unsure

Prefer the choice that keeps failure kinds distinguishable and the agent boundary structural. Write the test first when the behavior is specified. When it is not specified, stop and ask the PO; log the question in `docs/open_questions.md` if the answer has to wait.
