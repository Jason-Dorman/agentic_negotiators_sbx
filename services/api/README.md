# `services/api`

One FastAPI process. Responsibilities per module and the dependencies each is allowed are in
[architecture.md](../../docs/architecture.md) section 3.2; the import contract that CI enforces
is in [contributing.md](../../docs/contributing.md) section 1.1 and encoded in
[`.importlinter`](../../.importlinter).

Code lands across stage 2 of [build_plan.md](../../docs/build_plan.md), one sub-stage at a time.
Every module the import contract names exists as a package from stage 2.1, with a docstring naming
the sub-stage that fills it, so the contract is enforced from the first backend code rather than
switched on after the fact.

| Module | Stage | State |
|---|---|---|
| `db/` | 2.1 | Models, the initial migration, repositories behind protocols, the unit of work |
| `chain/`, `relay/`, `indexer/`, `projection/`, `config/` | 2.3 | The web3 adapter and codec, the durable outbox, the indexer, the projection, the chain settings |
| `observation/`, `agent_client/`, `validation/`, `turns/`, `controller/` | 2.4 | The observation builder, the agent client, the setup validator, the turn executor, the run controller; `composition.py` |
| `evidence/`, `metrics/`, `routes/` | 2.5 | The run resource and the export, the per-run metrics with RPC cost, the operator API and its event stream; `main.py`, `python -m api` |

## Persistence (`db/`)

Everything outside `db/` sees a `UnitOfWork` — one transaction, every repository bound to it — and
frozen records. Nothing outside it imports SQLAlchemy (`.importlinter`, `sessions-stay-in-db`).

```python
async with database.unit_of_work() as uow:  # commits on exit, rolls back on raise
    run = await uow.runs.get(run_id)
    events = await uow.chain_events.canonical_for_run(run_id)  # canonical rows only
```

Migrations ship inside the package, so an installed backend can apply them with nothing but
`DATABASE_URL`:

```sh
make migrate                                   # or: uv run python -m api.db.migrate upgrade
uv run python -m api.db.migrate downgrade base
```

The schema is [data_model.md](../../docs/data_model.md) section 3, and three tests hold the
document, the migration and the models together: `test_db_schema_matches_data_model.py` compares the
migrated database with a hand transcription of the document; `test_db_migrations.py` runs Alembic's
autogenerate comparison between the models and the migration and checks that the downgrade leaves
nothing behind; `test_db_constraints.py` attempts everything the schema must refuse and asserts each
refusal by constraint name.

## The chain (`chain/`, `relay/`, `indexer/`, `projection/`)

Composed the way the stage 2.4 controller will compose them — `tests/support/api_chain.py`
`Backend` is the reference:

```python
codec = ExchangeCodec(
    deployment.exchange_address, deployment.base_token_address, deployment.quote_token_address
)
chain = Web3ChainAdapter(settings.chain_rpc_url, timeout_s=settings.rpc_timeout_s)
relay = Relay(
    database,
    chain,
    codec,
    chain_id=deployment.chain_id,
    holder=process_id,
    relay_signer=LocalTransactionSigner.from_reference(settings.relay_key_ref, os.environ, "relay"),
    operator_signer=LocalTransactionSigner.from_reference(
        settings.operator_key_ref, os.environ, "operator"
    ),
    policy=settings.relay_policy(),
)
indexer = Indexer(
    database, chain, codec, deployment, TimelineSentences(), policy=settings.indexer_policy()
)

await relay.submit_action(run_id, signed_action_id)  # persist, then broadcast the stored bytes
report = await indexer.poll()  # reorgs, receipts, logs, depth, terminal
projection = await Projector(database, default_threshold=settings.confirmation_threshold).project(
    run_id
)  # session view, timeline, balances, outcome
await relay.reconcile(run_id)  # after a restart or a reorg
await relay.replace_stuck(run_id)  # ADR-050
```

Upper layers import `api.db.records`, `api.db.enums`, `api.db.errors` and `api.db.protocols`
directly, never the `api.db` package: its `__init__` imports `Database` and therefore SQLAlchemy,
which `sessions-stay-in-db` forbids outside `db/`, transitively. Tests may import `api.db`.

## The process and the operator API (`routes/`, `evidence/`, `metrics/`)

`python -m api` serves [api_contract.md](../../docs/api_contract.md) section 2 from the
environment's configuration (`infra/.env.example`); `api.main` is what it serves, and its lifespan
migrates when `AUTO_MIGRATE` is set, loads `DEPLOYMENT_MANIFEST` and `SCENARIOS_DIR`, builds the
graph once through `api.composition`, and starts `RunController.recover` in the background. In the
local Compose profile the image `services/api/Dockerfile` builds is the `api` service
([runbook.md](../../docs/runbook.md) section 8).

```python
backend = build_controller(
    database, deployment, chain_settings, controller_settings, environ=os.environ
)
services = build_services(backend, database, api_settings, software_version="0.1.0")
app = create_app(api_settings, services)  # what the integration suite serves through ASGITransport
```

`tests/support/api_http.py` `ApiHarness` is the reference: the same three calls over the stage 2.4
controller harness, and `serve` for a real uvicorn server where a test must read the event stream.

## Tests

Test layers and their directories are in [test_strategy.md](../../docs/test_strategy.md) section 2:

| Directory | Layer |
|---|---|
| `tests/unit/` | Fakes for `ModelClient`, `ChainAdapter`, `KeyHolder` and repositories |
| `tests/integration/` | Real PostgreSQL and Anvil |
| `tests/isolation/` | Information-boundary suite (A12) over requests, logs, SSE and exports |
| `tests/contract/` | OpenAPI snapshot against [api_contract.md](../../docs/api_contract.md) |
| `tests/support/` | Helpers imported by bare name (`from api_seed import Seeder`); on `pythonpath` and `mypy_path`, and deliberately without a `conftest.py` |

The integration suite needs PostgreSQL (`make up`). Without it the suite skips; with
`REQUIRE_INTEGRATION=1`, which CI and `make ci` set, the same absence is a failure. It uses the
`agent_negotiation_test` database the Compose profile creates, or `TEST_DATABASE_URL`.

Two of these modules are on the privacy-sensitive list in
[contributing.md](../../docs/contributing.md) section 1.2: `observation/` and `evidence/`, plus
`routes/sse.py`. A change to any of them requires a reviewer to walk the data classification
table in [data_model.md](../../docs/data_model.md) section 7.
