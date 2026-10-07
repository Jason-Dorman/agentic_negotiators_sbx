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
| 8. The whole stack, the operator API and the export | Stage 2.5; replay in stage 4 |
| 9. The model: its key, its price table and the smoke call | Stage 3.1; live runs in stage 3.4 |

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

`make up` is `docker compose --profile local up -d --wait postgres anvil`, so it returns only once
both services report healthy, and starts nothing else: the test suites need only these two. If it
returns an error about a port, read section 2. The whole application — the API and both agents — is
`make stack`, in section 8.

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

The transaction half is stage 2.3's relay and indexer; the run half — when recovery runs, what moves
a run to `RECOVERY_REQUIRED`, and how an operator brings one back — is the stage 2.4 controller's,
at the end of this section. Until stage 2.5 there are no routes, so the controller's operations are
called in-process; the integration suite is where every claim below was checked.

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

A run already `terminal` is not watched ([Q20](open_questions.md), deferred to stage 5), and
neither is one in `failed_setup`: its session never opened, or was aborted and its outcome
recorded.

### The run: what moves it, and how to bring it back

The controller drives one active run at a time ([ADR-019](decision_log.md)). A run in `preparing` or
`running` is polled every `INDEXER_POLL_INTERVAL_S`; a `paused` run only while something is still in
flight — a turn's action, a requested step, an abort. A paused run with nothing in flight costs no
RPC requests.

**The run's state and cause:**

```sh
psql "$DATABASE_URL" -c "
  SELECT state, state_cause, outcome_kind, outcome_reason_code FROM runs WHERE id = '<run id>'"
psql "$DATABASE_URL" -c "
  SELECT cursor, event_type, data FROM run_events WHERE run_id = '<run id>' ORDER BY cursor DESC LIMIT 20"
```

**What moves a run to `RECOVERY_REQUIRED`**, by `state_cause`. None of these is an outcome: the
outcome stays `pending`, and an unresolved fault is never recorded as a no-deal result (FR-E6).

| Cause | What happened | What to do |
|---|---|---|
| `rpc_timeout` | The RPC did not answer for `RPC_OUTAGE_LIMIT_S`, 60 s by default ([ADR-063](decision_log.md)). Also from `preparing`: an outage during setup | Check the endpoint (above). Resume once it answers |
| `agent_unavailable` | An agent did not answer — refused connection, timeout, a `503` from a signer that did not load — for as long ([ADR-064](decision_log.md)) | Check the agent's process and its log for `signer_unavailable`; resume once `GET /internal/health` answers with `signer_ok: true`. The turn is asked again with the same observation |
| `nonce_conflict`, `unreachable`, `refused` | A reconciliation needed a person: the outcome table above ([ADR-057](decision_log.md)) | As the table says; usually fund the relay, or find what used its key |
| `observation_inconsistent` | An agent refused five observations in a row as contradictory ([ADR-046](decision_log.md)). A backend defect or a persistently stale read | Compare the last turns' `observation` with the chain; resume rebuilds it again |
| `invalid_confirmation_threshold`, `session_opening_missing` | The indexer's `RunProblem` for the run ([ADR-059](decision_log.md)) | A defect in the run's configuration or the indexer's records; abort |
| `settlement_check_failed` | A settlement's receipt did not show exactly the two signed legs (architecture 5.3) | Do not resume. Reconstruct the session (section 5) and compare |
| `agent_<code>`, `observation_<code>` | An agent refused something the backend should never send, or the backend could not build an observation — the code says which ([ADR-064](decision_log.md)) | A defect; the turn is closed with the code as its `failure_code`. Resume once fixed, or abort |
| `termination_reverted` | An abort or expiry reverted and the session is still open before its deadline | Look at the transaction's `last_error`. The termination stays recorded; once the deadline passes an abort sends `expireSession` |
| `internal_error` | Something the driver did not expect; the exception is in the log line `run.driver_failed` | A defect. Resume once understood, or abort |
| `chain_unavailable` | The run's chain went away — an Anvil restart — and a backend serving another chain stranded it at start-up ([ADR-081](decision_log.md)) | Nothing can reach its session; its outcome stays unknown and every operation on it is refused. Its records and export are kept |

A run that ends `failed_setup` carries why in its cause: `<kind>_reverted` for a setup transaction that reverted (`mint_reverted`, `fund_eth_reverted`, `approve_reverted`, `create_session_reverted` — its `last_error` says why), `session_refused` for a session an agent refused ([ADR-066](decision_log.md)), or `abort_requested`.

**Resume from `RECOVERY_REQUIRED`** reconciles the run's outbox, polls once, and decides from the
chain: `terminal` if the session has ended there, `paused` (cause `recovered`) if the negotiation can
go on, or `preparing` if setup had not finished — setup then carries on from where it stopped, and
finds what it already sent in the outbox rather than sending it again ([ADR-063](decision_log.md)).
A run carried on from setup this way pauses once its session is open; resume it again to run.

**Abort** works from `preparing`, `running`, `paused` and `recovery_required`, so it is always the
way out of a run that cannot go on ([ADR-067](decision_log.md)). It is recorded on the run first
(`termination_cause`, `termination_code`), and the controller sends it ([ADR-068](decision_log.md)):
before the session's deadline `abortSession` from the operator key, which needs no participant key;
at or after it, `expireSession` from the relay. A run with no session yet is `failed_setup` at once,
with no RPC needed, and the active run is freed. Otherwise the run keeps its state until the
termination is canonical at the threshold; a settlement that lands first wins (spec 9.4). Abort is
also how to restart a recorded termination that stopped for a fault — a model failure's abort that
met an outage, say: the cause recorded first is kept. While a termination is recorded, resume and
step are refused ([ADR-070](decision_log.md)); to see one:

```sh
psql "$DATABASE_URL" -c "
  SELECT state, state_cause, termination_cause, termination_code FROM runs WHERE id = '<run id>'"
```

**A reorganisation** pauses a running run with cause `reorg` and reconciles at once; a turn in
flight completes. Resume reconciles again and continues. During setup the run stays `preparing`,
cause `reorg`, and pauses when its session is open rather than starting.

**After a backend restart**, `RunController.recover` waits until the old process's lease has
expired (`RUN_LEASE_TTL_S`, 30 s by default) — at once if the old process shut down gracefully,
which releases it — then reconciles the outbox, and drives the run again if it was `preparing` or
`running`; a `paused` run stays paused (architecture 5.4). A turn interrupted before its action was
recorded is asked again with the observation it stored, and the agent answers an identical request
with its first response — unless the observation has gone stale since, its offer or its session past
expiry, when the turn closes `observation_stale` and a fresh one is built
([ADR-071](decision_log.md)). One interrupted after is handed to the relay as it stands. Never a new
decision on the same state (FR-E2). Two processes never drive one run: a live lease is waited for,
not taken.

**An agent restarted** mid-run answers `unprovisioned` — or `provisioned`, when a restore stopped
between provisioning and approval. The controller re-provisions it with the stored address,
approves the session again from the canonical `SessionOpened`, and asks again
([ADR-048](decision_log.md)); during setup, before any session exists, it is provisioned again and
asked for its approval once more. The derived address must match or the run goes to
`RECOVERY_REQUIRED` with `agent_restore_address_mismatch`.

## 7. Funding a testnet demonstration

Stage 5 completes this section. What is known now, from stages 2.3 and 2.4, is what the indexer
costs on the RPC and what setup costs the operator.

**Each run's participant wallets are funded by the operator** with exactly the worst-case fee of
their setup approval — its gas limit times its fee cap ([ADR-065](decision_log.md)): about 0.0002
ETH per wallet at three gwei, so the operator key needs about 0.0004 Sepolia ETH a run for this,
plus its own gas for funding, minting and `createSession`. What a wallet does not spend stays in it;
nothing sweeps it back. An approval stuck past `RELAY_REPLACE_AFTER_BLOCKS` is signed again by its
agent with both fee caps up an eighth, and the wallet topped up to the new worst case first
([ADR-072](decision_log.md)); funding only ever grows. If the operator key cannot pay for a setup
transaction, the node refuses it and the run goes to `RECOVERY_REQUIRED`, cause `refused`: fund the
operator and resume, and setup sends the same bytes again.

**The Sepolia RPC must allow wide `eth_getLogs` ranges.** On every poll the indexer re-reads every
block above the finalized head — about 64 to 100 blocks on Sepolia — in one `eth_getLogs`
([ADR-053](decision_log.md)). Alchemy's free tier caps a call at 10 blocks, so with the default
`INDEXER_LOG_CHUNK_BLOCKS=2000` nothing is indexed on it; Pay As You Go allows the range. Which
plan and limit to use is [Q40](open_questions.md), still open.

**What it costs on Pay As You Go**, at $0.525 per million compute units (Alchemy's PAYG FAQ, checked
1 October 2026): about 220 compute units a poll, so about **$0.10 per hour of polling** at the
Sepolia poll interval of 4 s; a 30-minute run about $0.05; the relay's own calls under $0.001 an
action. Polling around the clock for a month would be about 145M compute units, about $75 — the
stage 2.4 controller polls only while a run is being driven, and a paused run with nothing in flight
is not polled at all (section 6). **Set a usage limit** in the Alchemy
dashboard (Billing Settings, as a compute-unit amount or its dollar value): usage stops at it and
nothing beyond it is charged. $5 a month is about 9.5M compute units, around 45 hours of active
polling.

## 8. The whole stack, the operator API and the export

Stage 2.5. Replay arrives with its interface in stage 4 ([ADR-074](decision_log.md)); the Sepolia
profile in stage 5.

### Starting it

The stack needs `infra/.env` with the local profile's keys and the two agents' shared secrets
(section 3):

```sh
cp infra/.env.example infra/.env
uv run --group tooling python infra/scripts/generate_keys.py --profile local   # paste the output in
python3 -c "import secrets; print(secrets.token_hex(32))"   # once for AGENT_A_SHARED_SECRET,
python3 -c "import secrets; print(secrets.token_hex(32))"   # once for AGENT_B_SHARED_SECRET
make stack
```

`make stack` is `docker compose --env-file infra/.env --profile local up -d --build --wait`. In
order: PostgreSQL and Anvil; `deploy`, which funds the relay and operator from Anvil's first
account and deploys the contracts, writing the manifest `local-compose` to the `deployments`
volume — or does nothing when that manifest's exchange still has code ([ADR-076](decision_log.md));
the two agents, healthy once each answers its own signed health check with its signer loaded; and
`api`, which applies the migrations, loads the manifest, checks the RPC is its chain, loads the
scenarios, and answers on `127.0.0.1:8000`.

**After an Anvil restart.** Anvil keeps no state, so its chain is a new one, with a new genesis
block, and `deploy` names its deployment from that block — `local-compose-<eight hex digits>` — so
the database keeps the old deployment and its runs apart from the new ([ADR-081](decision_log.md)).
A restart of Anvil alone is noticed by health: `rpc_ok` is false, because the genesis no longer
matches. Run `make stack` again — `deploy` deploys to the new chain — and restart the api, which
serves the new deployment:

```sh
make stack
docker compose --env-file infra/.env --profile local up -d --force-recreate api
```

A run that was in flight on the old chain cannot reach its session again: at start-up the api moves
it to `recovery_required`, cause `chain_unavailable`, its outcome left pending, and frees the
active-run slot. Every operation on it is then refused with `details.deployment`; its rows and its
export stay as they were. Old deployments stay listed by `GET /v1/deployments`.

**Stopping it.** `docker stop` (or `make down`) ends every event stream at once and gives the api
five seconds for anything else, inside its fifteen-second stop grace, so it releases its lease and
the next process takes the run over without waiting ([ADR-082](decision_log.md)).

```sh
curl -s localhost:8000/v1/health | python3 -m json.tool
docker compose --profile local logs deploy        # what the deployment did
docker compose --profile local logs -f api        # one JSON line per request; never a body
```

| Symptom | Cause |
|---|---|
| `deploy` exits with `RELAY_PRIVATE_KEY is not set` | `infra/.env` lacks the keys, or `make stack` was not used, so Compose read no env file |
| `deploy` exits with `the RPC ... did not answer` | Anvil is not up; `docker compose --profile local logs anvil` |
| `api` exits at start-up naming the manifest or `CHAIN_RPC_URL` | the RPC is not the manifest's chain — another chain ID, no exchange code at its address, or a deployment id loaded before against another genesis. Deploy again (`make stack`), which names the new chain's deployment apart |
| `api` exits naming `SCENARIOS_DIR` | the directory is missing, empty, or holds a file that fails `scenario.v1.json`; the message names the file and where, never what it holds |
| an agent stays `unhealthy` | its signer did not load — a root variable unset — or its shared secret is under 32 characters; its log says which |
| `api` restarts with `invalid controller configuration` | a variable is missing or a key was pasted where its reference belongs; the message names the variable, never the value |
| `/v1/health` is `503` | the body says which of `rpc_ok`, `db_ok`, `agent_a_ok`, `agent_b_ok` is false |

### Driving a run through the API

Every `POST` may carry `Idempotency-Key: <uuid>`; a retry with the same key and body is answered
with the first response, marked `Idempotent-Replayed: true`, and never does the work twice
([ADR-078](decision_log.md)). With `OPERATOR_TOKEN` set, every route but health needs
`Authorization: Bearer <token>`.

```sh
API=http://127.0.0.1:8000/v1
# A deterministic run of the default scenario: the body of api_contract section 2.2.
RUN=$(curl -s -X POST $API/runs -H 'Content-Type: application/json' -d @run.json \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
curl -s -X POST $API/runs/$RUN/validate | python3 -m json.tool      # every check, passing or not
curl -s -X POST $API/runs/$RUN/start -D - -o /dev/null | grep -i location   # 202, an operation
curl -s $API/operations/<operation id> | python3 -m json.tool        # running, then succeeded
curl -sN $API/runs/$RUN/events                                       # the live event stream
curl -s $API/runs/$RUN | python3 -m json.tool                        # state, timeline, metric strip
```

`step` takes one turn and leaves the run paused; `pause`, `resume` and `abort` are as section 6
describes. An operation records how its run's transition ended — `start` once the run is running,
`step` once its turn is done, `abort` once the run has ended, with whether the abort or a settlement
won ([ADR-073](decision_log.md)). A reconnecting stream sends `Last-Event-ID: <cursor>` and gets
everything after it.

### Private views

`GET /runs/{id}/mandates`, `/decisions`, `/scenarios/{id}`, a private export and the utilities in
`/metrics` need `X-Observer-Reveal: true`. Each access is logged as `observer.reveal`, with the
route and the run and never what was shown (ADR-018). The header is friction and audit; the operator
already holds everything.

### The export

```sh
curl -s $API/runs/$RUN/export -o run.export.json                    # public: private is null
curl -s "$API/runs/$RUN/export?include_private=true" -H 'X-Observer-Reveal: true' -o private.json
uv run python -c "
import json, sys
from negotiation_protocol import validate
validate(json.load(open(sys.argv[1])), 'export.v1.json'); print('export valid')
" run.export.json
```

The export is the archive of record: it keeps the chain rows a reorg invalidated, marked
`canonical: false`, and its metrics are recomputed when it is taken. To check it against the chain
with no database at all, run the reconstruction tool (section 5) on its `run.session.session_id`
with the manifest in its `deployment_manifest`; acceptance A15 does exactly that.

### The RPC price table

A run's RPC requests are counted by method while it is driven, and priced from the operator's
table ([ADR-061](decision_log.md), [ADR-075](decision_log.md)). The packaged table,
`services/api/src/api/config/rpc_prices.json`, prices Anvil at zero. To price a hosted provider,
copy it, add an entry — its price per million units, the units each method costs, a default for
unlisted methods or `null`, a `source` and a `last_verified` date — and point `RPC_PRICE_TABLE` at
the copy and `RPC_PROVIDER` at the entry's name. A provider the table does not list, or a method it
cannot price, shows its cost as unknown, never as zero. Stage 5 adds Sepolia's provider.

## 9. The model: its key, its price table and the smoke call

Stage 3.1 built the model client and the budget; stage 3.2 puts them behind the agent's
`ModelPolicy`, and stage 3.4 adds the procedure for a live model-versus-model run. Until then, an
agent instance offers the deterministic policy only.

### The key

Each agent instance has its own Anthropic API key, named by its own reference
([ADR-086](decision_log.md)): `AGENT_MODEL_KEY_REF=env:BUYER_ANTHROPIC_API_KEY` for the buyer's
instance, with the key in `BUYER_ANTHROPIC_API_KEY`, and the seller's counterparts. Only the `env:`
form is accepted; a `keystore:` reference, or a key pasted where its reference belongs, stops the
instance starting with a message that does not repeat it. So does a reference to a variable that
holds another secret — an `AGENT_` setting, a root, a private key, a shared secret, the keystore
password, or whatever variable `AGENT_ROOT_KEY_REF` names — and a value that does not begin
`sk-ant-`, so a slip cannot send a signing key to the provider (Q79). The client is handed the key
explicitly, so `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, an `ant auth login`
profile and the headers of `ANTHROPIC_CUSTOM_HEADERS` are never used, even when set, and it follows
no redirect. Two keys make each agent's spend visible separately in the
provider's usage reports; one key for both works, but the spend is then one figure.

### The price table

Every model call is priced from the model price table before it is sent and after it returns
([ADR-085](decision_log.md)). The packaged table, `services/agent/src/agent/budget/model_prices.json`,
prices `claude-sonnet-5-5` — the default — `claude-sonnet-5`, `claude-opus-5-5` and `claude-opus-5`
per million tokens of uncached input, output, cache reads, and 5-minute and 1-hour cache writes,
each with a `source` and a `last_verified` date. When a price changes, copy the file, edit the
entry and its `last_verified`, and point `AGENT_MODEL_PRICE_TABLE` at the copy; the agent service
reads it from stage 3.2, when it builds the client, and a model priced twice in one file is refused. A model the table
does not list is refused before any call is made; `AGENT_ALLOW_UNKNOWN_PRICE=true` lets it run with
only the call ceiling to stop it, and its cost then shows as unknown, never zero.

The two figures on a decision record are different things. *Estimated* is the call's worst case,
priced before it was sent: its counted input at the dearest input rate, and the full
`AGENT_MODEL_MAX_TOKENS` (16,000) of output — $0.16 at the default model's rates. *Reported* is what
the provider said the call used, priced from the same table, and is typically a few cents. The spend
ceiling counts the reported cost of past calls and the estimate of the next one, so the default
$2.00 ceiling admits a call while less than about $1.84 has been spent. A call that reported nothing
— a timeout, an error status, a malformed answer, a call cancelled at a deadline — is counted at its
estimate, because it may still have been billed. Its reported cost is unknown, and so is the run's
reported total: read the estimate beside it, which is always known for a priced model (Q81).

### The smoke call

One real call through the agent's own client, to show that the provider accepts the request it
builds and that usage and both costs come back. It costs about a cent, at most about two cents on
the default model (`max_tokens` is 2,000 for this call, under a $0.10 ceiling), and is never part
of `make ci`:

```bash
export BUYER_ANTHROPIC_API_KEY=sk-ant-...          # in this shell only, not infra/.env
make smoke-model KEY_REF=env:BUYER_ANTHROPIC_API_KEY
```

It prints the outcome, the model asked for and the model that answered, the stop reason, the usage,
both costs, the request id and the latency — never the key, the prompt or the answer — and exits 0
when the outcome is `decided`. Any other outcome exits 1 and says which:

| Outcome | What to do |
|---|---|
| `rejected`, 401 | The key is wrong or revoked |
| `rejected`, 404 | The model id is not one the key's workspace can use |
| `rejected`, 400 | The request shape was refused; the provider's error type is printed. Report it: the client builds the request |
| `rejected`, 3xx | Something between here and the provider redirected the call; it was not followed. Check for a proxy |
| `rate_limited` | Wait and run it again; nothing was retried |
| `provider_failure`, `connection_failed`, `timeout` | The provider or the network; run it again later |
| `budget_refused` | The model is not in the price table; the refusal's reason is printed |
| `refusal`, `max_tokens`, `unparseable` | The call worked and the model's answer was not a decision; the request shape was accepted |
