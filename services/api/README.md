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
| `chain/`, `relay/`, `indexer/`, `projection/`, `config/` | 2.3 | Packages only |
| `observation/`, `agent_client/`, `validation/`, `turns/`, `controller/` | 2.4 | Packages only |
| `evidence/`, `metrics/`, `routes/` | 2.5 | Packages only |

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
