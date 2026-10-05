#!/bin/sh
# The local Compose profile's one-shot `deploy` service (stage 2.5; ADR-076, ADR-081).
#
# The Compose Anvil keeps no state across a restart, so the contracts have to be deployed again
# each time it starts — and the deployment that results is a new one, on a new chain, even though
# Anvil's fixed mnemonic gives it the same addresses. This script, run by the `deploy` service before
# `api` starts:
#
#   1. reads the chain's genesis hash, which differs on every fresh Anvil, and names the deployment
#      `local-compose-<first eight hex digits of it>`, so a restarted chain gets a deployment id of
#      its own and the database keeps the old one, with its runs, apart (ADR-081);
#   2. funds the relay and the operator from Anvil's first unlocked account, every time, so keys
#      changed in infra/.env are funded too;
#   3. does nothing more if the manifest it wrote last time is this chain's deployment — the same
#      id, its exchange holding code, and the relay and operator it records being the keys given;
#   4. otherwise runs the real deploy script (contracts/script/Deploy.s.sol) with that account as
#      the deployer, and writes the manifest to the shared `deployments` volume as
#      `local-compose.json`, which the api service reads as DEPLOYMENT_MANIFEST.
#
# No private key is used to deploy or to fund: Anvil signs for its own unlocked accounts. The relay
# and operator keys are read only to learn their addresses; they are the local profile's throwaway
# keys (ADR-023). An RPC that does not answer is a failure, never "nothing to do".
set -eu

RPC_URL="${ANVIL_RPC_URL_CONTAINER:-http://anvil:8545}"
OUT_DIR="${DEPLOYMENTS_DIR:-/deployments}"
MANIFEST="${OUT_DIR}/local-compose.json"
# Anvil's first default account: public, unlocked, and funded with 10,000 test ETH.
FUNDER="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
# Each backend key gets this much test ETH when it holds less than half of it.
FUND_ETHER=100

: "${RELAY_PRIVATE_KEY:?RELAY_PRIVATE_KEY is not set: see infra/.env.example}"
: "${OPERATOR_PRIVATE_KEY:?OPERATOR_PRIVATE_KEY is not set: see infra/.env.example}"
RELAY_ADDRESS="$(cast wallet address --private-key "${RELAY_PRIVATE_KEY}")"
OPERATOR_ADDRESS="$(cast wallet address --private-key "${OPERATOR_PRIVATE_KEY}")"
unset RELAY_PRIVATE_KEY OPERATOR_PRIVATE_KEY

GENESIS="$(cast block 0 -f hash --rpc-url "${RPC_URL}")" || {
    echo "deploy: the RPC at ${RPC_URL} did not answer" >&2
    exit 1
}
DEPLOYMENT_ID="local-compose-$(printf '%s' "${GENESIS}" | cut -c3-10)"

for ADDRESS in "${RELAY_ADDRESS}" "${OPERATOR_ADDRESS}"; do
    # Whole ether: a wei balance does not fit the shell's integers.
    BALANCE="$(cast balance --ether "${ADDRESS}" --rpc-url "${RPC_URL}")"
    if [ "${BALANCE%%.*}" -lt "$((FUND_ETHER / 2))" ]; then
        cast send --unlocked --from "${FUNDER}" --value "${FUND_ETHER}ether" "${ADDRESS}" \
            --rpc-url "${RPC_URL}" >/dev/null
        echo "deploy: funded ${ADDRESS}"
    fi
done

field() {
    sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "${MANIFEST}" | head -n 1
}

lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

if [ -f "${MANIFEST}" ] && [ "$(field deployment_id)" = "${DEPLOYMENT_ID}" ] \
    && [ "$(lower "$(field relay_address)")" = "$(lower "${RELAY_ADDRESS}")" ] \
    && [ "$(lower "$(field operator_address)")" = "$(lower "${OPERATOR_ADDRESS}")" ]; then
    CODE="$(cast code "$(field exchange_address)" --rpc-url "${RPC_URL}")"
    if [ "${CODE}" != "0x" ]; then
        echo "deploy: ${DEPLOYMENT_ID} is this chain's deployment; nothing to do"
        exit 0
    fi
fi
echo "deploy: deploying ${DEPLOYMENT_ID}"

# Build and deploy from a private copy: the source is mounted read-only, and the script writes
# its manifest under ../docs/deployments relative to the project root (contracts/foundry.toml).
WORK="$(mktemp -d)"
cp -r /src/contracts "${WORK}/contracts"
mkdir -p "${WORK}/docs/deployments"
cd "${WORK}/contracts"
DEPLOYMENT_ID="${DEPLOYMENT_ID}" OPERATOR_ADDRESS="${OPERATOR_ADDRESS}" \
RELAY_ADDRESS="${RELAY_ADDRESS}" MANIFEST_OVERWRITE=true \
    forge script script/Deploy.s.sol:Deploy --rpc-url "${RPC_URL}" --broadcast \
    --unlocked --sender "${FUNDER}"
cp "${WORK}/docs/deployments/${DEPLOYMENT_ID}.json" "${MANIFEST}"
echo "deploy: wrote ${MANIFEST} for ${DEPLOYMENT_ID}"
