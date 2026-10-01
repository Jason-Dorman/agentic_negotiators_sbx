"""A stand-in for the backend and the relay, driving two real agent processes over HTTP.

Stage 2.2's exit condition needs the whole agent side working against a real chain before any of the
backend exists (docs/build_plan.md). This module plays the backend's part of the run — provision,
fund, broadcast the agents' own setup approvals, open the session, have both agents approve it,
build each turn's observation from chain state, relay each signed action — with web3 and nothing
else, so a failure points at the agent or the contract rather than at shared machinery.

It is deliberately *not* the backend's design in miniature. There is no outbox, no indexer and no
confirmation depth: Anvil mines each transaction as it arrives, and the receipt is the confirmation.
Those are stage 2.3's. What it does keep faithfully is the information boundary: each observation is
built for the acting party alone, from public chain state and that party's own previous decisions,
with no mandate in it — the agent injects its own — and nothing one agent says reaches the other.

Each agent runs as its own process through the real entry point, `python -m agent`, configured the
way Compose will configure it: by `AGENT_*` variables and a root key reference.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID, uuid4

import httpx
from anvil_chain import ANVIL_KEYS, free_port
from eth_account import Account
from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from web3 import Web3
from web3.contract.contract import Contract, ContractFunction

from negotiation_protocol import (
    AUTH_HEADER,
    REQUEST_ID_HEADER,
    agent_request_mac,
    load_abi,
    to_hex32,
)

ROLES: Final = ("buyer", "seller")
OPEN: Final = 1
SETTLED: Final = 2
CLOSED: Final = 3
ZERO_DIGEST: Final = b"\x00" * 32


def counterparty(role: str) -> str:
    return "seller" if role == "buyer" else "buyer"


# --------------------------------------------------------------------------------------
# Agent processes
# --------------------------------------------------------------------------------------


class AgentClient:
    """Signed HTTP calls to one agent instance (ADR-041)."""

    def __init__(self, base_url: str, secret: str) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=30.0)
        self._secret = secret.encode("utf-8")

    def call(self, method: str, path: str, body: Any = None) -> httpx.Response:
        content = b"" if body is None else json.dumps(body).encode("utf-8")
        headers = {
            AUTH_HEADER: agent_request_mac(self._secret, method, path, content),
            REQUEST_ID_HEADER: str(uuid4()),
        }
        return self._http.request(method, path, content=content, headers=headers)

    def ok(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        response = self.call(method, path, body)
        if response.status_code != 200:
            raise AssertionError(f"{method} {path} -> {response.status_code}: {response.text}")
        payload: dict[str, Any] = response.json()
        return payload

    def post(self, run_id: UUID, route: str, body: Any = None) -> dict[str, Any]:
        return self.ok("POST", f"/internal/runs/{run_id}/{route}", body)

    def close(self) -> None:
        self._http.close()


@dataclass(frozen=True)
class AgentProcess:
    role: str
    key_ref: str
    root_address: str
    client: AgentClient
    log_path: Path
    #: Every secret this process was given, in every form it could be printed, for the log scan.
    private_values: tuple[str, ...]


@contextmanager
def running_agent(
    *,
    role: str,
    instance: str,
    key_ref: str,
    root_address: str,
    secret: str,
    environment: Mapping[str, str],
    log_path: Path,
    private_values: tuple[str, ...],
) -> Iterator[AgentProcess]:
    """`python -m agent` on a free port, with exactly the environment Compose would give it."""
    port = free_port()
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AGENT_")
        and not name.endswith("_ROOT_KEY")
        and name != "KEYSTORE_PASSWORD"
    }
    env = {
        **inherited,
        "AGENT_ROLE": role,
        "AGENT_INSTANCE": instance,
        "AGENT_ROOT_KEY_REF": key_ref,
        "AGENT_SHARED_SECRET": secret,
        "AGENT_PORT": str(port),
        "AGENT_HOST": "127.0.0.1",
        **environment,
    }
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "agent"], env=env, stdout=log, stderr=subprocess.STDOUT
        )
        client = AgentClient(f"http://127.0.0.1:{port}", secret)
        try:
            _wait_for_health(client, process, log_path)
            yield AgentProcess(role, key_ref, root_address, client, log_path, private_values)
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover  reason: only on a wedged agent
                process.kill()


def _wait_for_health(
    client: AgentClient, process: subprocess.Popen[bytes], log_path: Path, timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"the agent exited with {process.returncode}:\n{log_path.read_text()}"
            )
        try:
            health = client.ok("GET", "/internal/health")
        except (httpx.TransportError, AssertionError):
            time.sleep(0.2)
            continue
        if not health["signer_ok"]:
            raise AssertionError(f"the agent's signer did not load:\n{log_path.read_text()}")
        return
    raise AssertionError(f"the agent did not answer within {timeout}s:\n{log_path.read_text()}")


# --------------------------------------------------------------------------------------
# The chain: operator and relay
# --------------------------------------------------------------------------------------


class Chain:
    """The operator's and the relay's view of the deployment. The relay signs transactions only."""

    def __init__(self, rpc_url: str, manifest: Mapping[str, Any]) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.manifest = manifest
        self.operator: LocalAccount = Account.from_key(ANVIL_KEYS[0])
        self.relay: LocalAccount = Account.from_key(ANVIL_KEYS[1])
        self.exchange = self._contract("exchange_address", "NegotiationExchange")
        self.base_token = self._contract("base_token_address", "MockERC20")
        self.quote_token = self._contract("quote_token_address", "MockERC20")

    def _contract(self, key: str, abi: str) -> Contract:
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.manifest[key]),
            abi=load_abi(abi),
            decode_tuples=True,
        )

    def chain_time(self) -> int:
        return int(self.w3.eth.get_block("latest")["timestamp"])

    def advance_time(self, seconds: int) -> None:
        """Anvil only: move chain time forward and mine a block at the new time."""
        self.w3.provider.make_request("evm_increaseTime", [seconds])  # type: ignore[arg-type]  # reason: Anvil-specific RPC methods are untyped in web3
        self.w3.provider.make_request("evm_mine", [])  # type: ignore[arg-type]  # reason: as above

    def fees(self) -> tuple[int, int]:
        base = int(self.w3.eth.get_block("latest").get("baseFeePerGas", 0) or 0)
        priority = 1_000_000_000
        return 2 * base + priority, priority

    def _receipt(self, tx_hash: HexBytes) -> dict[str, Any]:
        receipt = dict(self.w3.eth.wait_for_transaction_receipt(tx_hash))
        assert receipt["status"] == 1, f"transaction reverted: {tx_hash.hex()}"
        return receipt

    def send(self, account: LocalAccount, call: ContractFunction) -> dict[str, Any]:
        transaction = call.build_transaction(
            {"from": account.address, "nonce": self.w3.eth.get_transaction_count(account.address)}
        )
        signed = account.sign_transaction(cast("Any", transaction))
        return self._receipt(self.w3.eth.send_raw_transaction(signed.raw_transaction))

    def broadcast(self, raw_tx: str) -> dict[str, Any]:
        return self._receipt(self.w3.eth.send_raw_transaction(HexBytes(raw_tx)))

    def fund_eth(self, to: str, wei: int) -> None:
        max_fee, priority = self.fees()
        transaction: dict[str, Any] = {
            "type": 2,
            "chainId": self.w3.eth.chain_id,
            "nonce": self.w3.eth.get_transaction_count(self.operator.address),
            "to": to,
            "value": wei,
            "gas": 21_000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
        }
        # eth_account declares a narrower mapping than a plain dict; the data is the same.
        signed = self.operator.sign_transaction(cast("Any", transaction))
        self._receipt(self.w3.eth.send_raw_transaction(signed.raw_transaction))

    def mint(self, token: Contract, to: str, amount: int) -> None:
        if amount:
            self.send(self.operator, token.functions.mint(to, amount))

    def balances(self, address: str) -> dict[str, int]:
        return {
            "base": int(self.base_token.functions.balanceOf(address).call()),
            "quote": int(self.quote_token.functions.balanceOf(address).call()),
        }

    def open_session(
        self, buyer: str, seller: str, base_amount: int, duration: int, max_offers: int
    ) -> dict[str, Any]:
        """`createSession` by the operator: the decoded `SessionOpened` and its block's time."""
        session_id = secrets.token_bytes(32)
        expires_at = self.chain_time() + duration
        receipt = self.send(
            self.operator,
            self.exchange.functions.createSession(
                (session_id, buyer, seller, base_amount, expires_at, max_offers)
            ),
        )
        (event,) = self.exchange.events.SessionOpened().process_receipt(receipt)
        args = event["args"]
        block = self.w3.eth.get_block(receipt["blockNumber"])
        return {
            "session_id": to_hex32(args["sessionId"]),
            "buyer": args["buyer"],
            "seller": args["seller"],
            "base_token": args["baseToken"],
            "quote_token": args["quoteToken"],
            "base_amount_minor": str(args["baseAmount"]),
            "expires_at_ts": int(args["expiresAt"]),
            "max_offers": int(args["maxOffers"]),
            "config_hash": to_hex32(args["configHash"]),
            "opened_at_ts": int(block["timestamp"]),
        }

    def state(self, session_id: str) -> Any:
        return self.exchange.functions.getSession(bytes.fromhex(session_id[2:])).call()

    def offers(self, session_id: str) -> list[dict[str, Any]]:
        logs = self.exchange.events.OfferRecorded().get_logs(
            from_block=int(self.manifest["start_block"]),
            argument_filters={"sessionId": bytes.fromhex(session_id[2:])},
        )
        return [dict(log["args"]) for log in logs]

    def submit(self, action: Mapping[str, Any]) -> dict[str, Any]:
        """The relay: the signed message's fields and signature, as calldata. Signs the tx only."""
        message = action["typed_message"]
        signature = bytes.fromhex(action["signature"][2:])
        head = (
            bytes.fromhex(message["sessionId"][2:]),
            bytes.fromhex(message["configHash"][2:]),
            message["sequence"],
        )
        functions = self.exchange.functions
        if action["kind"] == "offer":
            offer = (*head, message["proposer"], int(message["quoteAmount"]), message["validUntil"])
            return self.send(self.relay, functions.recordOffer(offer, signature))
        if action["kind"] == "accept":
            accept = (*head, message["actor"], bytes.fromhex(message["offerHash"][2:]))
            return self.send(self.relay, functions.acceptAndSettle(accept, signature))
        closure = (*head, message["actor"], message["reason"])
        return self.send(self.relay, functions.closeSession(closure, signature))


# --------------------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------------------


@dataclass
class RunRecord:
    run_id: UUID
    addresses: dict[str, str]
    provisioned: dict[str, dict[str, Any]]
    setup_approvals: dict[str, dict[str, Any]]
    opened: dict[str, Any]
    approvals: dict[str, dict[str, Any]]
    balances_before: dict[str, dict[str, int]]
    turns: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    balances_after: dict[str, dict[str, int]] = field(default_factory=dict)
    final_status: int = OPEN

    @property
    def actions(self) -> list[tuple[str, dict[str, Any]]]:
        return [(role, response["signed_action"]) for role, response in self.turns]


class Backend:
    """Drives one run through two agents the way the controller will in stage 2.4."""

    def __init__(self, chain: Chain, agents: Mapping[str, AgentProcess]) -> None:
        self._chain = chain
        self._agents = agents

    def run(
        self,
        scenario: Mapping[str, Any],
        *,
        max_turns: int = 20,
        before_turn: Callable[[int, Chain], None] | None = None,
    ) -> RunRecord:
        run_id = uuid4()
        provisioned = {role: self._provision(run_id, role, scenario) for role in ROLES}
        addresses = {role: provisioned[role]["my_address"] for role in ROLES}
        self._fund(scenario, addresses)
        setup = {role: self._setup_approval(run_id, role, addresses[role]) for role in ROLES}
        before = {role: self._chain.balances(addresses[role]) for role in ROLES}
        public = scenario["public_config"]
        opened = self._chain.open_session(
            addresses["buyer"],
            addresses["seller"],
            int(public["base_amount_minor"]),
            int(public["session_duration_s"]),
            int(public["max_offers"]),
        )
        approvals = {
            role: self._agents[role].client.post(run_id, "approve-session", opened)
            for role in ROLES
        }
        record = RunRecord(run_id, addresses, provisioned, setup, opened, approvals, before)
        self._negotiate(record, scenario, max_turns, before_turn)
        record.balances_after = {role: self._chain.balances(addresses[role]) for role in ROLES}
        for role in ROLES:
            self._agents[role].client.post(run_id, "release")
        return record

    def _provision(self, run_id: UUID, role: str, scenario: Mapping[str, Any]) -> dict[str, Any]:
        public = scenario["public_config"]
        party = scenario[role]
        manifest = self._chain.manifest
        body = {
            "role": role,
            "policy": "deterministic",
            "model_id": None,
            "effort": None,
            "limits": {
                "model_call_ceiling": 20,
                "model_spend_ceiling_usd": "2.00",
                "model_timeout_s": 45,
                "repair_attempts": 1,
            },
            "expected_session": {
                "chain_id": int(manifest["chain_id"]),
                "exchange_address": manifest["exchange_address"],
                "base_token": manifest["base_token_address"],
                "quote_token": manifest["quote_token_address"],
                "base_amount_minor": public["base_amount_minor"],
                "max_offers": public["max_offers"],
                "session_duration_s": public["session_duration_s"],
                "offer_lifetime_s": public["offer_lifetime_s"],
            },
            "key_ref": self._agents[role].key_ref,
            "expected_address": None,
            "mandate_version_id": str(uuid4()),
            "mandate": party["mandate"],
            "initial_balances": party["initial_balances"],
            "allowance_minor": party["allowance_minor"],
        }
        return self._agents[role].client.post(run_id, "provision", body)

    def _fund(self, scenario: Mapping[str, Any], addresses: Mapping[str, str]) -> None:
        for role in ROLES:
            balances = scenario[role]["initial_balances"]
            self._chain.fund_eth(addresses[role], 10**18)
            self._chain.mint(self._chain.base_token, addresses[role], int(balances["base_minor"]))
            self._chain.mint(self._chain.quote_token, addresses[role], int(balances["quote_minor"]))

    def _setup_approval(self, run_id: UUID, role: str, address: str) -> dict[str, Any]:
        max_fee, priority = self._chain.fees()
        approval = self._agents[role].client.post(
            run_id,
            "setup-approval",
            {
                "nonce": self._chain.w3.eth.get_transaction_count(
                    Web3.to_checksum_address(address)
                ),
                "gas_limit": 70_000,
                "max_fee_per_gas_wei": str(max_fee),
                "max_priority_fee_per_gas_wei": str(priority),
            },
        )
        approval["receipt"] = self._chain.broadcast(approval["raw_tx"])
        return approval

    def _negotiate(
        self,
        record: RunRecord,
        scenario: Mapping[str, Any],
        max_turns: int,
        before_turn: Callable[[int, Chain], None] | None,
    ) -> None:
        previous: dict[str, list[dict[str, Any]]] = {role: [] for role in ROLES}
        for turn in range(1, max_turns + 1):
            if before_turn is not None:
                before_turn(turn, self._chain)
            state = self._chain.state(record.opened["session_id"])
            if state.status != OPEN:
                record.final_status = int(state.status)
                return
            role = self._next_actor(state, record.addresses)
            body = {
                **self._observation(record, role, state, previous[role]),
                "turn": turn,
                "deadline_at": _iso(datetime.now(UTC) + timedelta(seconds=45)),
            }
            record.observations.append(body)
            response = self._agents[role].client.post(record.run_id, "turn", body)
            assert response["status"] == "signed", response
            record.turns.append((role, response))
            record.receipts.append(self._chain.submit(response["signed_action"]))
            authorised = next(item for item in response["decisions"] if item["validation"]["ok"])
            previous[role].append(
                {
                    "turn": turn,
                    "decision": authorised["raw_response"]["decision"],
                    "result": "recorded",
                }
            )
        raise AssertionError(f"no terminal state after {max_turns} turns")

    def _next_actor(self, state: Any, addresses: Mapping[str, str]) -> str:
        """docs/protocol.md section 5, rule 3, from the contract's own state."""
        if state.offerCount == 0:
            return "buyer"
        proposer = next(role for role in ROLES if addresses[role] == state.activeProposer)
        return counterparty(proposer)

    def _observation(
        self,
        record: RunRecord,
        role: str,
        state: Any,
        previous: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The allowlisted observation of docs/protocol.md section 12, less the mandate."""
        opened = record.opened
        chain_time = self._chain.chain_time()
        roles_by_address = {address: name for name, address in record.addresses.items()}
        offers = self._chain.offers(opened["session_id"])
        own = sum(1 for offer in offers if roles_by_address[offer["proposer"]] == role)
        max_offers = int(opened["max_offers"])
        share = (max_offers + 1) // 2 if role == "buyer" else max_offers // 2
        manifest = self._chain.manifest
        return {
            "schema_version": "1",
            "run_id": str(record.run_id),
            "role": role,
            "my_address": record.addresses[role],
            "session": {
                "session_id": opened["session_id"],
                "config_hash": opened["config_hash"],
                "chain_id": int(manifest["chain_id"]),
                "exchange_address": manifest["exchange_address"],
                "base_token": manifest["base_token_address"],
                "quote_token": manifest["quote_token_address"],
                "base_amount_minor": opened["base_amount_minor"],
                "expires_at": opened["expires_at_ts"],
                "max_offers": max_offers,
                "token_decimals": 6,
            },
            "chain_time": chain_time,
            "expected_sequence": int(state.sequence) + 1,
            "offers_remaining_for_me": max(share - own, 0),
            "active_offer": _active_offer(state, roles_by_address, chain_time),
            "history": _history(offers, roles_by_address, chain_time),
            "my_balances": {
                "base_minor": str(self._chain.balances(record.addresses[role])["base"]),
                "quote_minor": str(self._chain.balances(record.addresses[role])["quote"]),
            },
            "my_previous_decisions": list(previous),
        }


def _active_offer(state: Any, roles: Mapping[str, str], chain_time: int) -> dict[str, Any] | None:
    if state.activeOfferHash == ZERO_DIGEST or int(state.activeValidUntil) <= chain_time:
        return None
    return {
        "offer_hash": to_hex32(state.activeOfferHash),
        "proposer": roles[state.activeProposer],
        "quote_amount_minor": str(state.activeQuoteAmount),
        "valid_until": int(state.activeValidUntil),
        "sequence": int(state.activeSequence),
    }


def _history(
    offers: list[dict[str, Any]], roles: Mapping[str, str], chain_time: int
) -> list[dict[str, Any]]:
    entries = []
    for index, offer in enumerate(offers):
        last = index == len(offers) - 1
        live = last and int(offer["validUntil"]) > chain_time
        entries.append(
            {
                "sequence": int(offer["sequence"]),
                "actor": roles[offer["proposer"]],
                "kind": "offer",
                "quote_amount_minor": str(offer["quoteAmount"]),
                "valid_until": int(offer["validUntil"]),
                "offer_hash": to_hex32(offer["offerHash"]),
                "status": "active" if live else ("expired" if last else "replaced"),
            }
        )
    return entries


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
