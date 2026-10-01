"""A real chain and deployment, with the backend's relay, indexer and projector wired to them.

The stage 2.3 integration tests (docs/build_plan.md) are claims about what the outbox, the indexer
and the projection do against a real chain, so they use one: the throwaway Anvil and the real deploy
script of `anvil_chain`, and the real PostgreSQL of this directory's conftest.

What this module stands in for is **setup**, which is stage 2.4's: it mints, funds and approves the
participants with web3 directly, and it signs each typed message with the participant's key the way
an agent would (the agent service's own signer is tested in stage 2.2). Everything from the signed
action onwards — the outbox row, the broadcast, the receipt, the event, the sentence, the session
view — goes through the code under test. Participants are fresh accounts for every session, so no
test inherits an allowance or a balance from another.

Shared helpers live here, in a named module on the `pythonpath`, never in a conftest
(docs/contributing.md section 3).
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from anvil_chain import ANVIL_KEYS
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import Hash32
from web3 import Web3
from web3.contract.contract import Contract, ContractFunction

from api.chain import ChainAdapter, ExchangeCodec, Web3ChainAdapter
from api.config import IndexerPolicy, RelayPolicy
from api.db import (
    ActionKind,
    Database,
    DeploymentRecord,
    NewDecision,
    NewRun,
    NewSignedAction,
    NewTurn,
    Party,
    PolicyKind,
    RunMode,
    RunRecord,
    SignedActionRecord,
    TurnState,
    TxKind,
)
from api.indexer import Indexer, PollReport
from api.projection import Projector, RunProjection, TimelineSentences
from api.relay import LocalTransactionSigner, Relay
from negotiation_protocol import (
    Accept,
    Address,
    Close,
    Digest,
    Domain,
    Offer,
    SessionConfig,
    SessionId,
    load_abi,
)

HOLDER: Final = "test-backend"
BASE_AMOUNT: Final = 10_000_000
BUYER_QUOTE: Final = 250_000_000
SELLER_BASE: Final = 25_000_000

PUBLIC_CONFIG: Final[dict[str, Any]] = {
    "base_amount_minor": str(BASE_AMOUNT),
    "max_offers": 8,
    "session_duration_s": 1800,
    "offer_lifetime_s": 600,
    "first_proposer": "buyer",
}


def deployment_from_manifest(manifest: dict[str, Any]) -> DeploymentRecord:
    document = {key: value for key, value in manifest.items() if key != "_path"}
    return DeploymentRecord(
        deployment_id=document["deployment_id"],
        chain_id=int(document["chain_id"]),
        protocol_version=document["protocol_version"],
        exchange_address=Address(document["exchange_address"]),
        base_token_address=Address(document["base_token_address"]),
        quote_token_address=Address(document["quote_token_address"]),
        operator_address=Address(document["operator_address"]),
        relay_address=Address(document["relay_address"]),
        code_hashes=document["code_hashes"],
        compiler=document["compiler"],
        explorer_base_url=document["explorer_base_url"],
        ens=document["ens"],
        manifest=document,
        start_block=int(document["start_block"]),
        deployed_at=datetime.fromtimestamp(int(document["deployed_at_ts"]), tz=UTC),
    )


# ---------------------------------------------------------------------------------------------
# The chain, through web3 directly: test controls and setup
# ---------------------------------------------------------------------------------------------


class AnvilChain:
    def __init__(self, rpc_url: str, manifest: dict[str, Any]) -> None:
        self.rpc_url = rpc_url
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.manifest = manifest
        self.operator: LocalAccount = Account.from_key(ANVIL_KEYS[0])
        self.relay: LocalAccount = Account.from_key(ANVIL_KEYS[1])
        self.outsider: LocalAccount = Account.from_key(ANVIL_KEYS[3])
        self.exchange = self._contract("exchange_address", "NegotiationExchange")
        self.base_token = self._contract("base_token_address", "MockERC20")
        self.quote_token = self._contract("quote_token_address", "MockERC20")
        self.domain = Domain(int(manifest["chain_id"]), manifest["exchange_address"])
        self.automining = True

    def _contract(self, key: str, abi: str) -> Contract:
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.manifest[key]), abi=load_abi(abi)
        )

    def rpc(self, method: str, *params: Any) -> Any:
        response = self.w3.provider.make_request(cast("Any", method), list(params))
        assert "error" not in response, f"{method}: {response['error']}"
        return response.get("result")

    # -- controls --------------------------------------------------------------------------------

    def mine(self, blocks: int = 1) -> None:
        self.rpc("anvil_mine", hex(blocks))

    def snapshot(self) -> str:
        return str(self.rpc("evm_snapshot"))

    def revert(self, snapshot_id: str) -> None:
        assert self.rpc("evm_revert", snapshot_id) is True

    def automine(self, on: bool) -> None:
        self.rpc("evm_setAutomine", on)
        self.automining = on

    def pooled(self) -> int:
        """Transactions in Anvil's pool not yet mined."""
        status = self.rpc("txpool_status")
        return int(status["pending"], 16) + int(status["queued"], 16)

    def block_gas_limit(self, limit: int) -> None:
        self.rpc("evm_setBlockGasLimit", hex(limit))

    def drop(self, tx_hash: str) -> None:
        self.rpc("anvil_dropTransaction", tx_hash)

    def advance_time(self, seconds: int) -> None:
        self.rpc("evm_increaseTime", hex(seconds))
        self.mine()

    def head(self) -> int:
        return int(self.w3.eth.block_number)

    def chain_time(self) -> int:
        return int(self.w3.eth.get_block("latest")["timestamp"])

    def block_hash(self, number: int) -> str:
        return "0x" + bytes(self.w3.eth.get_block(number)["hash"]).hex()

    def transactions_from(self, address: str, from_block: int) -> list[str]:
        """Every mined transaction sent by `address` at or above a height."""
        hashes = []
        for number in range(from_block, self.head() + 1):
            block = self.w3.eth.get_block(number, full_transactions=True)
            for tx in block["transactions"]:
                tx_data = cast("Any", tx)
                if tx_data["from"] == address:
                    hashes.append("0x" + bytes(tx_data["hash"]).hex())
        return hashes

    # -- setup, which is stage 2.4's, done here with web3 ----------------------------------------

    def send(self, account: LocalAccount, call: ContractFunction) -> dict[str, Any]:
        tx = call.build_transaction(
            {
                "from": account.address,
                "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
            }
        )
        signed = account.sign_transaction(cast("Any", tx))
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        assert receipt["status"] == 1
        return dict(receipt)

    def participants(self) -> tuple[LocalAccount, LocalAccount]:
        """A fresh buyer and seller, minted, funded with test ETH, and approving the exchange."""
        buyer: LocalAccount = Account.create()
        seller: LocalAccount = Account.create()
        for account in (buyer, seller):
            self.rpc("anvil_setBalance", account.address, hex(10**18))
        self.send(self.operator, self.quote_token.functions.mint(buyer.address, BUYER_QUOTE))
        self.send(self.operator, self.base_token.functions.mint(seller.address, SELLER_BASE))
        self.send(buyer, self.quote_token.functions.approve(self.exchange.address, BUYER_QUOTE))
        self.send(seller, self.base_token.functions.approve(self.exchange.address, SELLER_BASE))
        return buyer, seller

    def token_balances(self, address: str) -> tuple[int, int]:
        return (
            int(self.base_token.functions.balanceOf(address).call()),
            int(self.quote_token.functions.balanceOf(address).call()),
        )

    def session_state(self, session_id: str) -> dict[str, Any]:
        """`getSession`, as the projection's session view names it."""
        state = self.exchange.functions.getSession(bytes.fromhex(session_id[2:])).call()
        config, config_hash, status, sequence, offer_count, active_hash = state[:6]
        statuses = ["none", "open", "settled", "closed", "expired", "aborted"]
        return {
            "session_id": "0x" + bytes(config[0]).hex(),
            "config_hash": "0x" + bytes(config_hash).hex(),
            "buyer_address": config[1],
            "seller_address": config[2],
            "base_amount_minor": str(config[3]),
            "expires_at_ts": int(config[4]),
            "max_offers": int(config[5]),
            "status": statuses[int(status)],
            "sequence": int(sequence),
            "offer_count": int(offer_count),
            "active_offer_hash": "0x" + bytes(active_hash).hex(),
        }


# ---------------------------------------------------------------------------------------------
# Signing, as an agent would
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedMessage:
    kind: ActionKind
    sequence: int
    typed_message: dict[str, Any]
    digest: Digest
    signature: str
    signer: Address


@dataclass
class Session:
    run_id: uuid.UUID
    session_id: SessionId
    config_hash: Digest
    expires_at: int
    buyer: LocalAccount
    seller: LocalAccount
    domain: Domain
    turns: int = field(default=0)

    def party(self, account: LocalAccount) -> Party:
        return Party.BUYER if account.address == self.buyer.address else Party.SELLER

    def _head(self) -> tuple[bytes, bytes]:
        return bytes.fromhex(self.session_id[2:]), bytes.fromhex(self.config_hash[2:])

    def _signed(
        self, kind: ActionKind, account: LocalAccount, digest: bytes, typed: dict[str, Any]
    ) -> SignedMessage:
        signature = bytes(account.unsafe_sign_hash(Hash32(digest)).signature)
        return SignedMessage(
            kind=kind,
            sequence=int(typed["sequence"]),
            typed_message=typed,
            digest=Digest(digest),
            signature="0x" + signature.hex(),
            signer=Address(account.address),
        )

    def offer(
        self, account: LocalAccount, sequence: int, amount: int, valid_until: int
    ) -> SignedMessage:
        session_id, config_hash = self._head()
        message = Offer(session_id, config_hash, sequence, account.address, amount, valid_until)
        typed = {
            "sessionId": str(self.session_id),
            "configHash": str(self.config_hash),
            "sequence": sequence,
            "proposer": account.address,
            "quoteAmount": str(amount),
            "validUntil": valid_until,
        }
        return self._signed(ActionKind.OFFER, account, message.digest(self.domain), typed)

    def accept(self, account: LocalAccount, sequence: int, offer_hash: str) -> SignedMessage:
        session_id, config_hash = self._head()
        message = Accept(
            session_id, config_hash, sequence, account.address, bytes.fromhex(offer_hash[2:])
        )
        typed = {
            "sessionId": str(self.session_id),
            "configHash": str(self.config_hash),
            "sequence": sequence,
            "actor": account.address,
            "offerHash": offer_hash,
        }
        return self._signed(ActionKind.ACCEPT, account, message.digest(self.domain), typed)

    def close(self, account: LocalAccount, sequence: int, reason: int) -> SignedMessage:
        session_id, config_hash = self._head()
        message = Close(session_id, config_hash, sequence, account.address, reason)
        typed = {
            "sessionId": str(self.session_id),
            "configHash": str(self.config_hash),
            "sequence": sequence,
            "actor": account.address,
            "reason": reason,
        }
        return self._signed(ActionKind.CLOSE, account, message.digest(self.domain), typed)


# ---------------------------------------------------------------------------------------------
# The backend, composed the way stage 2.4's controller will compose it
# ---------------------------------------------------------------------------------------------


class Backend:
    def __init__(
        self,
        database: Database,
        chain: AnvilChain,
        *,
        adapter: ChainAdapter | None = None,
        relay_policy: RelayPolicy | None = None,
    ) -> None:
        self.database = database
        self.chain = chain
        self.deployment = deployment_from_manifest(chain.manifest)
        self.codec = ExchangeCodec(
            self.deployment.exchange_address,
            self.deployment.base_token_address,
            self.deployment.quote_token_address,
        )
        self.adapter: ChainAdapter = adapter or Web3ChainAdapter(chain.rpc_url)
        self.relay_policy = relay_policy or RelayPolicy()
        self.relay = self.new_relay()
        self.indexer = self.new_indexer()
        self.projector = Projector(database)

    def new_relay(self, adapter: ChainAdapter | None = None) -> Relay:
        environ = {"RELAY_KEY": ANVIL_KEYS[1], "OPERATOR_KEY": ANVIL_KEYS[0]}
        return Relay(
            self.database,
            adapter or self.adapter,
            self.codec,
            chain_id=self.deployment.chain_id,
            relay_signer=LocalTransactionSigner.from_reference("env:RELAY_KEY", environ, "relay"),
            operator_signer=LocalTransactionSigner.from_reference(
                "env:OPERATOR_KEY", environ, "operator"
            ),
            holder=HOLDER,
            policy=self.relay_policy,
        )

    def new_indexer(self) -> Indexer:
        return Indexer(
            self.database,
            self.adapter,
            self.codec,
            self.deployment,
            TimelineSentences(),
            policy=IndexerPolicy(),
        )

    async def poll(self) -> PollReport:
        """One indexer poll, after automine has mined what was sent.

        Anvil's automine can mine a transaction a moment after `send_raw` returns, and a poll
        records only blocks at or below the head it read (stage 2.3 CI failure): a test that sends
        and then polls once means "after it is mined". With automine off, a test holds
        transactions in the pool on purpose, and nothing waits.
        """
        if self.chain.automining:
            for _ in range(200):
                if self.chain.pooled() == 0:
                    break
                await asyncio.sleep(0.025)
            else:
                raise AssertionError("automine left transactions in the pool for 5 s")
        return await self.indexer.poll()

    async def project(self, session: Session) -> RunProjection:
        return await self.projector.project(session.run_id)

    async def open_session(
        self, *, threshold: int = 1, max_offers: int = 8, duration: int = 1800
    ) -> Session:
        """A run, its lease, and a session the operator creates through the relay."""
        buyer, seller = self.chain.participants()
        session_id = SessionId(secrets.token_bytes(32))
        expires_at = self.chain.chain_time() + duration
        config = SessionConfig(
            bytes.fromhex(session_id[2:]),
            buyer.address,
            seller.address,
            BASE_AMOUNT,
            expires_at,
            max_offers,
        )
        config_hash = Digest(
            config.config_hash(
                self.deployment.base_token_address, self.deployment.quote_token_address
            )
        )
        run = await self._run(threshold, max_offers)
        async with self.database.unit_of_work() as uow:
            # The controller writes the session id before `createSession` is broadcast, so the
            # indexer can attribute `SessionOpened` to the run (build_plan stage 2.4).
            await uow.runs.set_session(
                run.id, session_id, config_hash, expires_at, datetime.now(UTC)
            )
            await uow.leases.acquire(
                run.id, HOLDER, timedelta(minutes=5), datetime.now(UTC), relay_nonce_floor=0
            )
        await self.relay.submit_call(
            run.id,
            TxKind.CREATE_SESSION,
            self.deployment.exchange_address,
            self.codec.encode_create_session(config),
            as_operator=True,
        )
        return Session(
            run.id, session_id, config_hash, expires_at, buyer, seller, self.chain.domain
        )

    async def _run(self, threshold: int, max_offers: int) -> RunRecord:
        async with self.database.unit_of_work() as uow:
            await uow.deployments.upsert(self.deployment)
            return await uow.runs.add(
                NewRun(
                    name="stage 2.3 run",
                    deployment_id=self.deployment.deployment_id,
                    public_config={
                        **PUBLIC_CONFIG,
                        "max_offers": max_offers,
                        "confirmation_threshold": threshold,
                    },
                    limits={"model_call_ceiling": 20, "repair_attempts": 1},
                    buyer_policy=PolicyKind.DETERMINISTIC,
                    seller_policy=PolicyKind.DETERMINISTIC,
                    software_version="0.1.0+test",
                    mode=RunMode.FIXTURE,
                )
            )

    async def record(self, session: Session, message: SignedMessage) -> SignedActionRecord:
        """The rows a turn leaves before the relay: turn, authorised decision, signed action."""
        party = Party.BUYER if message.signer == Address(session.buyer.address) else Party.SELLER
        session.turns += 1
        async with self.database.unit_of_work() as uow:
            turn = await uow.turns.add(
                NewTurn(
                    run_id=session.run_id,
                    turn=session.turns,
                    party=party,
                    expected_sequence=message.sequence,
                    state=TurnState.SIGNING,
                    observation={"turn": session.turns},
                    observation_hash="0x" + secrets.token_hex(32),
                )
            )
            decision = await uow.decisions.add(
                NewDecision(
                    turn_id=turn.id,
                    run_id=session.run_id,
                    party=party,
                    attempt=1,
                    policy=PolicyKind.DETERMINISTIC,
                    raw_response={"decision": {"action": str(message.kind)}},
                    validation_ok=True,
                    requested_at=datetime.now(UTC),
                    authorized=True,
                )
            )
            return await uow.signed_actions.add(
                NewSignedAction(
                    run_id=session.run_id,
                    turn_id=turn.id,
                    decision_id=decision.id,
                    sequence=message.sequence,
                    kind=message.kind,
                    typed_message=message.typed_message,
                    digest=message.digest,
                    signer=message.signer,
                    signature=message.signature,
                )
            )

    async def act(self, session: Session, message: SignedMessage) -> SignedActionRecord:
        """Record a signed action and relay it."""
        action = await self.record(session, message)
        await self.relay.submit_action(session.run_id, action.id)
        return action


def export_errors(definition: str, instance: Any) -> list[str]:
    """Where `instance` fails `export.v1.json#/$defs/<definition>`: the shape the export carries."""
    from negotiation_protocol import validator_for

    schema = {"$ref": f"export.v1.json#/$defs/{definition}"}
    validator = validator_for("export.v1.json").evolve(schema=schema)
    return [error.message for error in validator.iter_errors(instance)]
