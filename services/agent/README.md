# `services/agent`

One codebase, two runtime instances configured with a role, a mandate, a signing key and model
credentials. Module responsibilities are in [architecture.md](../../docs/architecture.md)
section 3.3; the internal API it serves is [api_contract.md](../../docs/api_contract.md)
section 6.

The two rules that shape everything here:

- **The signer builds the message.** A decision from a policy is validated against the mandate
  and public state, and the typed message is then constructed from that validated state — never
  from policy-supplied bytes ([protocol.md](../../docs/protocol.md) section 11).
- **The mandate never leaves the instance.** It reaches this service once, at provisioning, and
  is discarded on release. It does not reach the other instance, the backend's logs, SSE or the
  default export ([protocol.md](../../docs/protocol.md) section 12).

## Running one

```sh
AGENT_ROLE=buyer AGENT_INSTANCE=agent-a AGENT_PORT=8101 \
AGENT_ROOT_KEY_REF=env:BUYER_ROOT_KEY BUYER_ROOT_KEY=0x… AGENT_SHARED_SECRET=… \
  uv run python -m agent
```

The variables, what a failed key load looks like and how to check one are in
[runbook.md](../../docs/runbook.md) section 3.

In the local Compose profile (stage 2.5) the image `services/agent/Dockerfile` builds runs as both
`agent-a` and `agent-b`, each given only its own root and shared secret. Its health check is
`python -m agent.healthcheck`, which signs `GET /internal/health` with the instance's own secret —
the route requires the HMAC like every other — and passes only when the instance answers with its
signer loaded ([runbook.md](../../docs/runbook.md) section 8).

## What is where

| Path | What |
|---|---|
| `routes/` | The six internal endpoints, HMAC over method, path and body first (ADR-041) |
| `service.py` | The use cases behind them; no HTTP and no key |
| `turns/` | Decide, validate, repair at most as provisioned, sign (ADR-012) |
| `policy/` | `Policy`, and `DeterministicPolicy`: the baseline of protocol section 13 |
| `validation/` | `MandateValidator`: every code in protocol section 11.1, with private feedback |
| `signing/` | Session approval, typed messages from validated state, the setup `approve` (ADR-040) |
| `keys/` | The instance's root from `env:` or `keystore:`, and each run's key derived from it (ADR-039) |
| `state/`, `observation.py` | Run-scoped memory; a typed view of a schema-valid observation |
| `consistency.py` | Refuse an observation that contradicts itself or the approved session (ADR-046) |
| `keys/references.py` | The grammar a key reference must satisfy, so a pasted key is refused (ADR-049) |
| `model/` | `ModelClient` and its Anthropic implementation: the request, the decision envelope, every way a call ends as one `ModelOutcome`, the key by `env:` reference (stage 3.1, ADR-086) |
| `budget/` | `BudgetGuard`, the call and spend ceilings before each call, and the model price table (stage 3.1, ADR-085) |
| `settings.py`, `logs.py`, `main.py` | `AGENT_*` configuration, redacted JSON logs, the composition root |
| `tools/smoke_model.py` | One real model call through the client, `make smoke-model` (runbook section 9) |

`prompts/` will hold versioned prompt templates, changed through review like any other source,
with the version hash recorded per decision ([ADR-029](../../docs/decision_log.md)). The model
client and the budget are built (stage 3.1); the model policy that uses them arrives in stage 3.2.

## Tests

`tests/unit/` runs in process with fakes. `tests/integration/test_negotiation_on_anvil.py` is stage
2.2's exit condition: two instances as real processes, driven over HTTP by a stand-in for the
backend and the relay, negotiate both scenarios on Anvil through the real contract.
`tests/unit/test_model_client.py` drives the model client against a local server that answers as
the provider does (`tests/support/fake_anthropic.py`); nothing in the suite reaches the network.
The validator
and the signer are held to 100 percent of branches (`make coverage-python`).
