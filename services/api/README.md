# `services/api`

One FastAPI process. Responsibilities per module and the dependencies each is allowed are in
[architecture.md](../../docs/architecture.md) section 3.2; the import contract that CI enforces
is in [contributing.md](../../docs/contributing.md) section 1.1 and encoded in
[`.importlinter`](../../.importlinter).

Code lands in stage 2 of [build_plan.md](../../docs/build_plan.md). Test layers and their
directories are in [test_strategy.md](../../docs/test_strategy.md) section 2:

| Directory | Layer |
|---|---|
| `tests/unit/` | Fakes for `ModelClient`, `ChainAdapter`, `KeyHolder` and repositories |
| `tests/integration/` | Real PostgreSQL and Anvil |
| `tests/isolation/` | Information-boundary suite (A12) over requests, logs, SSE and exports |
| `tests/contract/` | OpenAPI snapshot against [api_contract.md](../../docs/api_contract.md) |

Two of these modules are on the privacy-sensitive list in
[contributing.md](../../docs/contributing.md) section 1.2: `observation/` and `evidence/`, plus
`routes/sse.py`. A change to any of them requires a reviewer to walk the data classification
table in [data_model.md](../../docs/data_model.md) section 7.
