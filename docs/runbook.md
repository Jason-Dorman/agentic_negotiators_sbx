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
| 2. Database isolation and the ports; applying the schema; the integration suite | Stage 0; stage 2.1 |
| 3. Keys and secrets; running an agent instance | Stage 0 (generation); stage 2.2 (loading, roots, running an agent) |
| 4. Deploying the contracts and reading the manifest | Stage 1 |
| 5. Reconstructing a session from chain data | Stage 1 |
| 6. Recovering pending transactions | Stage 2.3 (transactions); stage 2.4 (runs) |
| 7. Funding a testnet demonstration | Stage 5 |
| 8. Replay and export | Stages 4 and 5 |

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

### If a tool is "not found"

`make` and the pre-commit hooks resolve the toolchain themselves — the Makefile prepends the
install directories and the hooks go through `infra/scripts/toolchain.sh` — so a commit works
the same from a terminal, an IDE's git integration or any other launcher with a short `PATH`.
Neither depends on your shell being configured.

Your own shell is a separate matter. To run `forge`, `cast`, `anvil` or `uv` directly, put the
install directories on your interactive `PATH`, once:

```sh
echo 'export PATH="$HOME/.local/bin:$HOME/.foundry/bin:$PATH"' >> ~/.bashrc && exec bash
```

If a hook still reports a tool missing after that, it is genuinely not installed; the hook's
error names the install command, and `make setup` follows it.

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

### Applying the schema — stage 2.1

The migrations ship inside the backend package, so applying them needs the installed package and a
URL and nothing else:

```sh
make migrate                                     # uses DATABASE_URL from infra/.env
DATABASE_URL=postgresql+asyncpg://… uv run python -m api.db.migrate upgrade
uv run python -m api.db.migrate downgrade base   # removes every table, enum and function
```

The local profile will run `upgrade` automatically when the backend starts (stage 2.5); on a Sepolia
host it is this command, run by hand, before the backend starts ([data_model.md](data_model.md)
section 8). Without `DATABASE_URL` the command refuses and exits 2 rather than guessing a database.

The `Makefile` reads `DATABASE_URL` and `TEST_DATABASE_URL` from `infra/.env` when that file exists,
and nothing else from it.

### Running the integration suite

The integration tests migrate and use the `agent_negotiation_test` database that
`infra/postgres/init` creates the first time the volume is initialised. `make up` first:

```sh
make up
uv run pytest services/api/tests/integration     # skips, with the reason, if PostgreSQL is down
make ci                                          # fails instead: it sets REQUIRE_INTEGRATION=1
```

**A skip is a local convenience, never a pass.** Without PostgreSQL a plain `pytest` skips the
integration suite and says why; CI and `make ci` set `REQUIRE_INTEGRATION=1`, which turns the same
absence into a failure, because a gate that skips reports green without having run.

The suite reaches the database at `TEST_DATABASE_URL`, defaulting to the Compose defaults (role,
password and database `agent_negotiation`, host port 55432, database `agent_negotiation_test`). If
you copied `infra/.env.example` *before* the volume was first created, the password is the one in
that file; set `TEST_DATABASE_URL` to match, or run `make reset-db` and start again. The migration
round-trip test also creates and drops a scratch database, `agent_negotiation_migrations_scratch`,
which needs a role allowed to `CREATE DATABASE` — the Compose role is.

---

## 3. Keys and secrets

Every key in this project is a test-network key. None of this is production custody, and the
security document says so in those words ([security_and_trust_boundaries.md](security_and_trust_boundaries.md)
section 7).

### Generating keys

Once per deployment, not per run: every run's participant wallets are derived from the two agents'
roots ([ADR-039](decision_log.md)), so there is nothing to regenerate between runs.

```sh
# Local profile: throwaway secrets as `env:` refs. No password needed.
uv run --group tooling python infra/scripts/generate_keys.py --profile local

# Sepolia profile: encrypted web3 keystores written to infra/secrets/.
# Set the password in your shell, not on the command line, so it stays out of shell history.
read -rs KEYSTORE_PASSWORD && export KEYSTORE_PASSWORD
uv run --group tooling python infra/scripts/generate_keys.py --profile sepolia
```

Four secrets, of two kinds. The **relay** and **operator** keys are used as they are, so the script
prints their addresses: those are the addresses to fund. The **buyer** and **seller** secrets are
**roots** (`BUYER_ROOT_KEY`, `SELLER_ROOT_KEY`): a root never signs and is never funded, so no address
is printed for one. The local branch prints the values for pasting into `infra/.env`; the Sepolia
branch writes `relay.json`, `operator.json`, `buyer-root.json` and `seller-root.json` into
`infra/secrets/` and prints only `keystore:` references.

### Loading keys and running an agent — stage 2.2

Each agent instance resolves its root once, at start-up, through the `KeyHolder` in
`services/agent/src/agent/keys/`, from the reference in `AGENT_ROOT_KEY_REF`:

| Reference | Resolves by | Profile |
|---|---|---|
| `env:BUYER_ROOT_KEY` | reading that variable: 32 bytes of hex, with or without `0x` | local |
| `keystore:/run/secrets/buyer-root.json` | decrypting the file with `KEYSTORE_PASSWORD` | Sepolia |

For each run it derives a fresh key from the root, the chain ID, its role and the run ID, and reports
only the address. The backend stores that address and the derivation's public inputs in `wallets`;
it never sees a key of either kind.

To run an instance by hand (Compose does this from stage 2.5), give the process its own values and
nothing else. Do not `source infra/.env` into its shell: that file holds both roots, and each root
reaches exactly one instance.

```sh
from_env() { grep "^$1=" infra/.env | cut -d= -f2-; }
env -i PATH="$PATH" HOME="$HOME" \
  AGENT_ROLE=buyer AGENT_INSTANCE=agent-a AGENT_PORT=8101 \
  AGENT_ROOT_KEY_REF=env:BUYER_ROOT_KEY BUYER_ROOT_KEY="$(from_env BUYER_ROOT_KEY)" \
  AGENT_SHARED_SECRET="$(from_env AGENT_A_SHARED_SECRET)" \
  uv run python -m agent
```

The seller is the same with `seller`, `agent-b`, `8102`, `SELLER_ROOT_KEY` and
`AGENT_B_SHARED_SECRET`; on Sepolia, `AGENT_ROOT_KEY_REF=keystore:/run/secrets/seller-root.json` and
`KEYSTORE_PASSWORD` in place of the root variable. Every setting an instance reads is `AGENT_`-prefixed, apart from the
variable its root reference names and `KEYSTORE_PASSWORD`:

| Variable | Required | Notes |
|---|---|---|
| `AGENT_ROLE` | yes | `buyer` or `seller` |
| `AGENT_INSTANCE` | yes | `agent-a` or `agent-b`; lowercase letters, digits and hyphens |
| `AGENT_ROOT_KEY_REF` | yes | `env:NAME` (an upper-case variable name) or `keystore:/path.json`. Anything else — a key pasted here, or after the prefix as in `env:BUYER_ROOT_KEY=0x…` — stops the instance starting, and the error does not print it ([ADR-049](decision_log.md)) |
| `AGENT_SHARED_SECRET` | yes | At least 32 characters. The HMAC key of the internal API, one per instance ([ADR-041](decision_log.md)) |
| `AGENT_PORT` | yes | 8101 and 8102 by convention |
| `AGENT_HOST` | no | `127.0.0.1` by default |
| `AGENT_SETUP_GAS_LIMIT_MAX` | no | 100000 by default ([ADR-042](decision_log.md)) |
| `AGENT_SETUP_MAX_COST_WEI` | no | 10000000000000000 (0.01 ETH) by default: the most a setup approval may cost, gas limit times fee cap ([ADR-047](decision_log.md)) |
| `AGENT_LOG_LEVEL` | no | `INFO` by default |

**Checking it loaded.** An instance whose root did not resolve still starts — so "the agent is down"
and "the agent's key is misconfigured" stay distinguishable — reports `"signer_ok": false` from its
health route, logs `signer_unavailable` with the reason, and refuses to provision with
`503 dependency_unavailable`. The health route needs a signed request like every other:

```sh
AGENT_A_SHARED_SECRET="$(from_env AGENT_A_SHARED_SECRET)" uv run python - <<'PY'
import os, uuid, httpx
from negotiation_protocol import AUTH_HEADER, REQUEST_ID_HEADER, agent_request_mac
secret = os.environ["AGENT_A_SHARED_SECRET"].encode()
headers = {AUTH_HEADER: agent_request_mac(secret, "GET", "/internal/health", b""),
           REQUEST_ID_HEADER: str(uuid.uuid4())}
print(httpx.get("http://127.0.0.1:8101/internal/health", headers=headers).json())
PY
```

| `signer_unavailable` reason | What to do |
|---|---|
| `the environment variable BUYER_ROOT_KEY is not set` | The reference names a variable the instance's environment lacks. Set it, or correct the reference |
| `… is not 32 bytes of hex` | The variable holds something other than a 32-byte secret |
| `KEYSTORE_PASSWORD is not set, so the keystore … cannot be opened` | Export the password in the instance's environment |
| `the keystore … cannot be read` / `… is not a keystore: it is not JSON text` | The path is wrong, not mounted, or not a keystore file |
| `the keystore … could not be decrypted with KEYSTORE_PASSWORD` | Wrong password for that file |

**After an agent restart.** A restarted instance has forgotten every run and answers the next call
for one with `409 invalid_state`, state `unprovisioned`. From stage 2.4 the controller recovers on
its own: it re-provisions the run with the address it stored, which the derivation reproduces, re-sends
approve-session from the canonical `SessionOpened` event and that block's timestamp, and retries
([ADR-048](decision_log.md)). Until then, nothing to do by hand.

**Changing or losing a root.** A new root derives different addresses for every future run, which is
harmless: wallets are fresh per run anyway. It cannot sign for a run that is still open under the old
root, because that run's address was derived from the old one. If a root is lost while a run is open,
the recovery is an operator abort, which needs no participant key ([ADR-039](decision_log.md)).

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

## 4. Deploying the contracts and reading the manifest

One invocation produces one deployment and one manifest, and the manifest is the only thing
downstream components are allowed to trust about that deployment's identity.

```sh
# Local, against the Compose Anvil. The deployer is also the operator here.
DEPLOYMENT_ID=local-$(date +%Y-%m-%d)-01 \
OPERATOR_ADDRESS=0x… \
RELAY_ADDRESS=0x… \
forge script contracts/script/Deploy.s.sol:Deploy \
  --root contracts \
  --rpc-url "$ANVIL_RPC_URL" \
  --broadcast \
  --private-key "$DEPLOYER_PRIVATE_KEY"
```

Note the two paths. The script path is resolved against your **working directory**, while `--root`
tells Foundry where the project is — which is what `fs_permissions` and the `out/` read are relative
to. From the repository root that means `contracts/script/...` together with `--root contracts`;
`script/Deploy.s.sol` with `--root contracts` fails with a bare `No such file or directory`. From
inside `contracts/` both shorten to `script/Deploy.s.sol:Deploy` with no `--root` at all.

| Variable | Required | Notes |
|---|---|---|
| `DEPLOYMENT_ID` | yes | Names the manifest file. Convention `<profile>-<date>-<nn>`, lowercase and hyphenated |
| `OPERATOR_ADDRESS` | yes | Immutable on all three contracts. The only address that may create or abort a session, or mint |
| `RELAY_ADDRESS` | yes | Recorded for the record, not granted anything. The relay pays gas and signs no message |
| `EXPLORER_BASE_URL` | no | `https://sepolia.etherscan.io` on Sepolia; leave unset locally and the manifest records `null` |
| `MANIFEST_DIR` | no | Defaults to `../docs/deployments`, relative to `contracts/` |
| `MANIFEST_OVERWRITE` | no | `true` to replace an existing manifest. Without it the script refuses, because a manifest is the only record of the deployment it describes |

**Two things are refused before a single transaction is broadcast.** A `DEPLOYMENT_ID` that would
not satisfy the manifest schema's `^[a-z0-9]+(-[a-z0-9]+)*$` — uppercase, an underscore, a doubled
or trailing hyphen — and an id whose manifest already exists. Both would otherwise leave a
deployment on chain that cannot be recorded, or replace the record of an earlier one.

**One case cannot be refused, so it is called out on the script's own output.** The manifest is
written during simulation, before Foundry broadcasts. If the broadcast then fails, the file is
already on disk and describes a deployment that never landed — **delete it**. A quick check:

```sh
# The addresses in the manifest must actually hold code.
cast code "$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['exchange_address'])" \
  docs/deployments/local-2026-09-24-01.json)" --rpc-url "$ANVIL_RPC_URL" | head -c 12
```

**After it runs, read the manifest before doing anything else with it.**

```sh
cat docs/deployments/local-2026-09-24-01.json
uv run python -c "
import json, sys
from negotiation_protocol import validate
validate(json.load(open(sys.argv[1])), 'deployment_manifest.v1.json')
print('manifest valid')
" docs/deployments/local-2026-09-24-01.json
```

Three things to know about the file ([ADR-038](decision_log.md)):

- **`start_block` is a lower bound**, not the exact deployment block. A `forge script` simulates
  against the current head and broadcasts afterwards, so on a fresh Anvil it reads 0 while all
  three contracts land in the next block. That is what a log scan needs.
- **`deployed_at_ts` is chain time**, in integer seconds, not the clock of the machine you ran this
  on.
- **Local manifests are git-ignored; Sepolia manifests are committed.** An Anvil chain lives as long
  as its container, so a committed local manifest records addresses on a chain that no longer exists.

The script deliberately does **not** mint, approve or resolve ENS. Funding belongs to a run, not to a
deployment — a deployment that pre-funded wallets would make "fresh test wallets per run" untrue —
and ENS names are resolved once, by hand, at registration and pinned into the manifest beside the
address they resolved to.

### If the script fails

| Symptom | Cause |
|---|---|
| `No such file or directory (os error 2)`, with nothing else | The script path is relative to your working directory, not to `--root`. See the note above |
| `the path … is not allowed to be accessed for write operations` | `MANIFEST_DIR` is outside `fs_permissions` in `contracts/foundry.toml`. Write inside `docs/deployments` or `contracts/out`, or add the path deliberately |
| `InvalidTokenPair` | The two token addresses are the same. The constructor refuses it ([ADR-037](decision_log.md)) |
| `DEPLOYMENT_ID must match …` | The id would not satisfy the manifest schema. Lowercase, digits and single internal hyphens only |
| `a manifest already exists at …` | That id has been deployed before. Use a new one, or `MANIFEST_OVERWRITE=true` if replacing it is what you mean |
| A manifest with one key in it | A `vm.serializeJson` regression. It *sets* an object's contents rather than adding to them; nested values go in with `vm.writeJson(value, path, ".key")` |

## 5. Reconstructing a session from chain data

The question this answers: with the application database gone, can the economic outcome of a run
still be established? Acceptance A15, and the procedure is
[protocol.md](protocol.md) section 14.

```sh
uv run python packages/protocol/tools/reconstruct.py \
  --rpc-url "$ANVIL_RPC_URL" \
  --manifest docs/deployments/local-2026-09-24-01.json \
  --session-id 0x… \
  --json reconstruction.json
```

It reads the chain and the manifest. Nothing else: not the database, not an export, not a run record.
Each check is printed with the evidence for it, and there are **three** exit statuses, because a
gate has to be able to tell a verdict from the absence of one:

| Status | Meaning |
|---|---|
| 0 | Every check ran and passed. The chain supports this settlement |
| 1 | Checks ran and at least one failed. Read which ones — the table below says what each implies |
| 2 | The tool could not reach a verdict: unreadable or invalid manifest, malformed session id, unreachable RPC. **Not** a failed check |

A reconstruction that recorded no checks at all also reports failure rather than success, for the
same reason: `all([])` is true, and "the tool never looked" must not read as "the chain agrees".

```
session 0xf13c…
outcome  settled (SettlementCompleted)
  seq 1  offer   signed by 0x3C44… submitted by 0x7099…
  seq 2  offer   signed by 0x90F7… submitted by 0x7099…
  seq 3  accept  signed by 0x3C44… submitted by 0x7099…

  [ok  ] config_hash: emitted 0xc7e2…, recomputed 0xc7e2…
  [ok  ] signatures_recover_to_the_named_actor: all recovered
  [ok  ] the_relay_signed_nothing: relay appears only as a submitter
  [ok  ] sequence_chain: [1, 2, 3] against the expected [1, 2, 3]
  [ok  ] settlement_quote_leg: 1 transfer(s) of 92000000 quote from buyer to seller
  …
RECONSTRUCTED
```

`web3` is behind the `tools` extra, so the tool needs `uv sync --all-extras` (or `make setup`). That
is deliberate: the agent service depends on this package and must not acquire an RPC client by doing
so ([ADR-035](decision_log.md)).

**Reading a failure.** `RECONSTRUCTION FAILED` names the checks that did not pass, and which ones
they are tells you where to look.

| Failing check | What it means |
|---|---|
| `chain_id`, `*_code_hash` | The manifest does not describe this chain. Wrong file, wrong RPC, or a redeployment |
| `session_opened` | No `SessionOpened` for that session id at or after `start_block` |
| `config_hash`, `session_base_token` | The manifest's token pair is not the pair the session was opened with |
| `signatures_recover_to_the_named_actor` | A recorded action's signature does not belong to the party the event names. This is the serious one |
| `the_relay_signed_nothing` | Gas payment and trading authority have been combined somewhere |
| `sequence_chain` | An action exists that the reconstruction cannot see, or one was recorded out of order |
| `no_settlement_moved_no_tokens` | A session with no settlement event nevertheless moved tokens |
| `settlement_amount_was_signed` | The settled digest matches no recorded offer, so there is no signed amount to hold the transfers to |
| `settlement_event_matches_the_signed_amount` | The event announced one amount and the offer was signed for another. The contract misreported |
| `settlement_quote_leg`, `settlement_base_leg` | The tokens that moved are not the amounts that were **signed for**. This is the one that means value moved on authority nobody gave |

## 6. Recovering pending transactions

The transaction half is stage 2.3's relay and indexer, described here. The run half — when recovery
runs, and what moves a run to `RECOVERY_REQUIRED` — is the controller's, and is added in stage 2.4.
Until then the controller is not built, so nothing in a running backend triggers these steps; they
are exercised by the integration suite, which is where every claim below was checked.

### What the outbox holds

Every transaction the backend sends is signed, written to `tx_outbox` and committed **before** it
is broadcast, and a retry sends the same stored bytes again — never a re-signed transaction and
never a new decision ([architecture.md](architecture.md) section 5.2). So after any crash the
database knows exactly what was about to be sent, and its hash. To see a run's transactions:

```sh
psql "$DATABASE_URL" -c "
  SELECT kind, status, nonce, tx_hash, block_number, attempts, last_error, replaces_id IS NOT NULL AS replacement
    FROM tx_outbox WHERE run_id = '<run id>' ORDER BY created_at"
```

(`DATABASE_URL` without its `+asyncpg` driver suffix for `psql`.)

| Status | Meaning | Who moves it on |
|---|---|---|
| `pending` | Persisted, not yet accepted by a node — or the process died before recording that it was | The relay's recovery |
| `submitted` | A node accepted it | The indexer, when a receipt appears; the relay, if it is stuck (below) |
| `included` | Its receipt is in a block below the run's confirmation threshold | The indexer |
| `confirmed` | At the threshold | The indexer, at the finalized head |
| `finalized` | At or below the RPC's finalized head | Nobody: final |
| `reverted` | Receipt status 0. `last_error` is the decoded protocol error and `sentence` the timeline text ([ADR-051](decision_log.md)) | Nobody; the controller requests an abort (spec 9.4) |
| `replaced` | Re-signed at a higher fee; its successor's `replaces_id` points here ([ADR-050](decision_log.md)) | The indexer, if this original is mined after all |
| `dropped` | Another transaction at the same sender and nonce was mined | Nobody |

### What recovery does, transaction by transaction

For each `pending` or `submitted` transaction of the run, the relay looks before it sends, in this
order ([architecture.md](architecture.md) section 5.4):

| Finding | Outcome | What it means for the operator |
|---|---|---|
| A receipt exists | `mined` | Nothing to do. The indexer records it on its next poll; nothing is sent again. This is the A13 case: a crash after the broadcast and before its record |
| The node still holds it | `in_pool` | Nothing to do; it is recorded as `submitted` and left to be mined |
| Neither, and its nonce is still free, and the node accepted the stored bytes | `rebroadcast` | They were sent again. Same bytes, same hash: if they had in fact been mined, the node would refuse them as a used nonce |
| Neither, its nonce free, and the node **refused** the bytes | `refused` | **Act.** The result's `detail`, the row's `last_error` and the `relay.broadcast_refused` log line say why — usually the relay is out of test ETH. Sending again will not help ([ADR-057](decision_log.md)) |
| Neither, and a transaction this outbox recorded at the same nonce was mined | `superseded` | The other one won — usually a replacement and its original. This one is marked `dropped`; the indexer records the winner on its next poll, and the signed action is never signed into a second transaction |
| Neither, and its nonce was used by a transaction the outbox does **not** know | `nonce_conflict` | **Act.** Something else signed with that key. See below |
| The RPC did not answer — before or during the resend | `unreachable` | **Act.** Nothing is known. An unresolved outage is `RECOVERY_REQUIRED`, never a no-deal result (FR-E6) |

**A nonce conflict** means the relay key — or the operator key — was used outside this backend. Find
the transaction that took the nonce:

```sh
cast nonce --rpc-url "$RPC" <relay address>          # how many the chain says are mined
cast tx    --rpc-url "$RPC" <tx_hash from tx_outbox>  # "not found" for ours
```

and look at the sender's history on the explorer for the transaction at that nonce. The signed
action this row carried was never executed. Do not reuse a relay or operator key in any other tool
or process; the one-run-at-a-time design (ADR-019) assumes the backend is the key's only user. The
way out of the run is an operator abort, which needs no participant key.

**An unreachable RPC**: check the endpoint (`cast block-number --rpc-url "$RPC"`), and on Sepolia the
provider's dashboard for throttling ([architecture.md](architecture.md) section 8). Recovery is
safe to repeat once it answers: every step looks up before it sends. A call waits `RPC_TIMEOUT_S`
and no longer — web3's own retries are off ([ADR-060](decision_log.md)) — and a rate limit is
always treated as an outage, never as the node refusing a transaction. The indexer treats an
unanswered block lookup the same way: the poll fails, and no reorg is inferred from it.

### A stuck transaction, and the fee ceiling

A transaction the relay or operator signed that is not included `RELAY_REPLACE_AFTER_BLOCKS` blocks
(default 3) after its first broadcast is replaced automatically: the same nonce, recipient,
calldata and gas, with both fee caps raised by an eighth. The original becomes `replaced` and the
successor points back to it ([ADR-050](decision_log.md)). Two things can stop this:

- **The ceiling.** No fee cap is ever above `RELAY_MAX_FEE_PER_GAS_WEI` (default 100 gwei), and a
  bump that the ceiling would clip below the node's 10 percent minimum is not made. The relay then
  waits. On Sepolia that means the network's base fee is above the ceiling: wait, or raise the
  setting and restart the backend.
- **A transaction an agent signed.** A participant's setup approval ([ADR-040](decision_log.md))
  cannot be replaced: the relay does not hold that key. Rebroadcast still applies to it.

To read what a stored transaction says — its nonce, gas and fee caps — without its signature:

```sh
uv run python -c "from api.relay import decode_raw_transaction; import sys; \
  print(decode_raw_transaction(bytes.fromhex(sys.argv[1])))" <raw_tx hex from tx_outbox>
```

### An action the node predicted would revert

The relay broadcasts it anyway, at `RELAY_FALLBACK_GAS_LIMIT` (default 500,000), so the contract
decides and the failure has a receipt ([ADR-054](decision_log.md)). The row ends `reverted` with
the protocol error in `last_error`, and the signed action `reverted` with the same name in
`revert_error`. The error is read by replaying the transaction at its own block and then at the
block before; `NoRevertData` means a replay reverted with nothing to decode (out of gas, say), and
`Undetermined` that neither replay reverted at all, so the reason cannot be named
([ADR-056](decision_log.md)). This costs relay test ETH; keep the relay funded. The reverted transaction's error
says why: `SequenceMismatch` or `StaleOfferDigest` is a stale view of the session, `OfferExpired` or
`SessionDeadlinePassed` is time running out, `ERC20InsufficientAllowance` a setup fault.

### After a reorganisation

The indexer detects it by block hash, marks the removed events non-canonical, moves the removed
transactions back to `submitted` and invalidates the balance snapshots above the fork
([ADR-053](decision_log.md)), and writes a `chain.reorg` run event for each affected run
([ADR-058](decision_log.md)); recovery then sends the removed transactions' stored bytes again, and
they land on the new fork. A reverted transaction removed this way loses its recorded error and its
timeline entry with the block. If a removed block comes back, its rows are made canonical again
rather than recorded twice ([ADR-055](decision_log.md)). To see the run event:

```sh
psql "$DATABASE_URL" -c "
  SELECT cursor, data FROM run_events WHERE run_id = '<run id>' AND event_type = 'chain.reorg'"
```

The rows themselves: The removed events stay in `chain_events` with `canonical = false` and
an `invalidated_at` time, and the export shows them: a reorg that happened is part of the record.
To see one:

```sh
psql "$DATABASE_URL" -c "
  SELECT event_name, block_number, block_hash, canonical, invalidated_at
    FROM chain_events WHERE run_id = '<run id>' ORDER BY block_number, log_index, created_at"
```

A run already `terminal` is not watched ([Q20](open_questions.md), deferred to stage 5).

## 7. Funding a testnet demonstration

Stage 5 completes this section. What is known now, from stage 2.3, is what the indexer costs on the
RPC.

**The Sepolia RPC must allow wide `eth_getLogs` ranges.** On every poll the indexer re-reads every
block above the finalized head — about 64 to 100 blocks on Sepolia — in one `eth_getLogs`
([ADR-053](decision_log.md)). Alchemy's free tier caps a call at 10 blocks, so with the default
`INDEXER_LOG_CHUNK_BLOCKS=2000` nothing is indexed on it; Pay As You Go allows the range. Which
plan and limit to use is [Q40](open_questions.md), still open.

**What it costs on Pay As You Go**, at $0.525 per million compute units (Alchemy's PAYG FAQ, checked
1 October 2026): about 220 compute units a poll, so about **$0.10 per hour of polling** at the
Sepolia poll interval of 4 s; a 30-minute run about $0.05; the relay's own calls under $0.001 an
action. Polling around the clock for a month would be about 145M compute units, about $75 — the
stage 2.4 controller polls only while a run is active. **Set a usage limit** in the Alchemy
dashboard (Billing Settings, as a compute-unit amount or its dollar value): usage stops at it and
nothing beyond it is charged. $5 a month is about 9.5M compute units, around 45 hours of active
polling.

## 8. Replay and export

Stages 4 and 5.
