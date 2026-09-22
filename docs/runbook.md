# Operator Runbook

| | |
|---|---|
| **Version** | 0.1.0 |
| **Date** | 22 September 2026 |
| **Status** | Living document. Started in stage 0 at the product owner's direction; grows in every stage and is completed in stage 5 ([build_plan.md](build_plan.md)) |
| **Related** | [build_plan.md](build_plan.md), [architecture.md](architecture.md), [security_and_trust_boundaries.md](security_and_trust_boundaries.md), [contributing.md](contributing.md) |

What an operator does, in the order they do it. Every procedure here is one a test or a stage
has actually exercised; nothing is written ahead of the code that makes it true.

| Section | Available from |
|---|---|
| 1. Local startup | Stage 0 |
| 2. Database isolation and the ports | Stage 0 |
| 3. Keys and secrets | Stage 0 (generation); stage 2 (loading) |
| 4. Recovering pending transactions | Stage 2 |
| 5. Funding a testnet demonstration | Stage 5 |
| 6. Replay and export | Stages 4 and 5 |

---

## 1. Local startup

Prerequisites, at the versions in [ADR-033](decision_log.md): `uv`, Node 22 with Corepack,
Foundry, Docker with Compose.

```sh
make setup      # uv sync, pnpm install, pre-commit install
make up         # PostgreSQL and Anvil, waits for both health checks
make ci         # every gate the build has earned so far
make down       # stop, keeping the database
```

`make up` is `docker compose --profile local up -d --wait`, so it returns only once both
services report healthy. If it returns an error about a port, read section 2.

Configuration is optional at this stage: the Compose profile runs on its own defaults. When a
stage needs configuration, copy the template and edit it.

```sh
cp infra/.env.example infra/.env
```

`infra/.env` is git-ignored. `infra/.env.example` carries placeholders only.

### Checking the stack is really up

```sh
docker compose --profile local ps
docker compose exec postgres psql -U agent_negotiation -d agent_negotiation -c '\l'
cast chain-id --rpc-url http://127.0.0.1:8545      # 31337
```

---

## 2. Database isolation and the ports

This project's PostgreSQL is namespaced away from every other stack on the host. Four things
make that true, and all four are deliberate:

| | Value | Why |
|---|---|---|
| Compose project | `agent_negotiation` | Namespaces every container, network and volume Compose creates |
| Volume | `agent_negotiation_postgres_data` | An explicit name, not a generic key under a project prefix |
| Database and role | `agent_negotiation` | Not `postgres`, so a stray connection to the wrong server fails rather than succeeding against someone else's data |
| Published host port | `55432` by default | 5432 is frequently taken by another project or a native install |

### Host port versus service name

The two are independent and confusing them is the mistake this section exists to prevent.

| Caller | Address | Notes |
|---|---|---|
| From the host: `psql`, an IDE, a migration run from your shell | `127.0.0.1:${POSTGRES_PORT}` | Default 55432 |
| From inside the Compose network: the `api` service, from stage 2 | `postgres:5432` | Service name and container port |

The container always listens on 5432. `POSTGRES_PORT` moves the published host side only, and
no application code reads it, so changing it is never a code change. The same rule holds for
Anvil: `127.0.0.1:8545` from the host, `anvil:8545` from inside.

### Changing the host port

Edit `POSTGRES_PORT` in `infra/.env`, keep the port in `DATABASE_URL` and `TEST_DATABASE_URL`
in step with it, then `make down && make up`. 5432 works if it is free on your machine.

### When start-up fails with a port error

```
Error response from daemon: ports are not available: exposing port TCP 127.0.0.1:5432
```

Something else already holds that host port. Find it, then either stop it or move this project:

```sh
ss -ltnp | grep -E ':5432|:55432|:8545'
docker ps --format '{{.Names}}\t{{.Ports}}'
```

### Resetting the database

```sh
make down        # stops the stack, keeps the data
make reset-db    # stops the stack and destroys agent_negotiation_postgres_data
```

`make reset-db` is the only command here that destroys data, and it destroys only this
project's volume. Renaming `POSTGRES_DB`, `POSTGRES_USER` or the Compose project name on an
existing installation has the same practical effect — the volume still holds the old database —
so treat a rename as a reset.

---

## 3. Keys and secrets

Every key in this project is a test-network key. None of this is production custody, and the
security document says so in those words ([security_and_trust_boundaries.md](security_and_trust_boundaries.md)
section 7).

### Generating keys — available now

```sh
# Local profile: throwaway keys as `env:` refs. No password needed.
uv run --group tooling python infra/scripts/generate_keys.py --profile local

# Sepolia profile: encrypted web3 keystores written to infra/secrets/.
# Set the password in your shell, not on the command line, so it stays out of shell history.
read -rs KEYSTORE_PASSWORD && export KEYSTORE_PASSWORD
uv run --group tooling python infra/scripts/generate_keys.py --profile sepolia
```

The local branch prints the keys for pasting into `infra/.env`. The Sepolia branch prints
addresses and `keystore:` references and never prints a private key.

### Loading keys — stage 2

Runtime loading is the `KeyHolder` in `services/agent/src/agent/keys/`, which arrives with the
agent service in stage 2 ([ADR-023](decision_log.md)). Both reference forms resolve through it:
`env:NAME` reads the variable, `keystore:/path` decrypts the file with `KEYSTORE_PASSWORD`.

The boundary it has to hold, from stage 2 onwards and unchanged by anything later:

- A signing key stays inside the agent service process. It never reaches a model prompt, the
  browser bundle, an evidence export, an ordinary log line, the database, or the other agent
  instance.
- The key holder exposes signing, not the key. Nothing retrieves key material through it.
- A key reaches configuration as a reference, never as a value. `infra/.env.example` documents
  the references; the secret scan rejects the values.

### What is checked automatically

The pre-commit hook and the CI secret-scan job run `infra/scripts/secret_scan.py`, which
rejects hex private keys on key-named lines, `sk-ant-` credentials, and keystore JSON anywhere
in the tree. `infra/scripts/check_spdx.py` enforces [ADR-032](decision_log.md) on Solidity
sources. Run either by hand:

```sh
uv run python infra/scripts/secret_scan.py
uv run python infra/scripts/check_spdx.py
```

---

## 4. Recovering pending transactions

Stage 2. The procedure lands in the same pull request as the recovery path it describes
([build_plan.md](build_plan.md) working agreements).

## 5. Funding a testnet demonstration

Stage 5.

## 6. Replay and export

Stages 4 and 5.
