# Two-Agent Negotiation and Settlement Sandbox

An observable experiment in agent negotiation and financial execution. Two independently instructed AI agents negotiate the price of a fixed quantity of a mock asset. Every valid public action is recorded on an Ethereum testnet by a non-upgradeable smart contract, and the agreed exchange settles atomically with test tokens, or not at all.

**Status:** governance documents complete; software and contracts not yet built. See [docs/build_plan.md](docs/build_plan.md).

## What it demonstrates

1. Two agents pursue separate objectives without seeing each other's private limits.
2. Offers and counteroffers are genuine model decisions, not a scripted price.
3. The negotiation can be reconstructed from contract events and calldata alone.
4. Both parties authorize the exact economic terms with linked EIP-712 signatures.
5. Settlement moves both token legs in one transaction or reverts entirely.
6. Walk-away, expiry, operator abort, model failure, and infrastructure failure are distinct, recorded outcomes.
7. A presenter can replay any run and explain the agreement, the authority chain, and the balances.

## What it does not claim

All assets are test tokens and all economics are simulated. The operator controls both agents on one host, so this is process isolation for an experiment, not independent counterparties. A successful run proves a working negotiation and authorization protocol. It does not prove customer demand, trading performance, or production security.

## How it works

```mermaid
flowchart LR
    OP["Operator"] --> WEB["React UI"]
    WEB --> API["Backend<br/>controller, relay, indexer"]
    API --> A["Agent A (buyer)<br/>own mandate, own key"]
    API --> B["Agent B (seller)<br/>own mandate, own key"]
    A --> LLM["Claude API"]
    B --> LLM
    API --> DB[("PostgreSQL")]
    API --> CHAIN["NegotiationExchange<br/>Anvil or Sepolia"]
```

The operator sets each agent's private mandate and starts a run. On each turn the backend sends the active agent an allowlisted observation. The agent's policy (a live model or a deterministic baseline) returns one of three decisions: offer, accept, or walk away. A policy signer inside the agent service validates the decision against the mandate and public state, constructs the typed message itself, and signs it. The relay submits it. The contract verifies signer, role, sequence, configuration hash, and expiry before recording it, and on acceptance transfers both legs in the same transaction.

## Stack

| Part | Choice |
|---|---|
| Interface | React, TypeScript, Vite |
| Backend and agents | Python 3.12, FastAPI, Pydantic, SQLAlchemy, web3.py |
| Model | Anthropic Claude via the official SDK |
| Contracts | Solidity, OpenZeppelin, Foundry |
| Database | PostgreSQL |
| Chains | Anvil (31337) locally, Ethereum Sepolia (11155111) for public demos |
| Local infra | Docker Compose |

## Repository layout (planned)

| Path | Contents |
|---|---|
| `apps/web/` | React application and typed API client |
| `services/api/` | FastAPI backend: run controller, chain relay, indexer |
| `services/agent/` | Agent service; run as two instances |
| `packages/protocol/` | JSON schemas, ABI artifacts, EIP-712 fixtures, reconstruction tool |
| `contracts/` | Solidity sources, Foundry tests, deployment scripts |
| `scenarios/` | Manufactured scenario templates and evaluation populations |
| `infra/` | Compose profiles and environment templates |
| `docs/` | Specification, governance documents, runbook, deployment manifests, evidence |

## Documentation

Start at [docs/README.md](docs/README.md). The originating specification is [docs/Agent-Negotiation-Sandbox-System-Spec-v0.1.md](docs/Agent-Negotiation-Sandbox-System-Spec-v0.1.md). Contributors read [docs/contributing.md](docs/contributing.md) before every change; AI assistants read [CLAUDE.md](CLAUDE.md).

## Getting started

Not yet runnable. Stage 0 of the build plan adds the toolchain, Compose profile, and CI. This section will then describe local startup; the operator runbook will cover funding a testnet demonstration, recovery, replay, and export.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Every Solidity source carries `// SPDX-License-Identifier: Apache-2.0` as its first line.

All assets, balances, prices, and mandates in this project are manufactured experiment inputs on test networks. Nothing here is a financial product or a claim about real economic value.

## Governance in one paragraph

Docs and code move together: any change to protocol, API, schema, behaviour, or a requirement updates the affected documents in the same change set. Decisions are recorded in [docs/decision_log.md](docs/decision_log.md). Gaps are raised with the product owner in [docs/open_questions.md](docs/open_questions.md) rather than assumed.
