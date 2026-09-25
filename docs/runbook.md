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
| 4. Deploying the contracts and reading the manifest | Stage 1 |
| 5. Reconstructing a session from chain data | Stage 1 |
| 6. Recovering pending transactions | Stage 2 |
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

Stage 2. The procedure lands in the same pull request as the recovery path it describes
([build_plan.md](build_plan.md) working agreements).

## 7. Funding a testnet demonstration

Stage 5.

## 8. Replay and export

Stages 4 and 5.
