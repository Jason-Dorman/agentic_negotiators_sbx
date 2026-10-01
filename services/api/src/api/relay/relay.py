"""The relay: every transaction is signed, persisted, and only then broadcast.

docs/architecture.md section 5.2 and spec section 9.2, steps 5 and 6. The order is the whole point:

1. **Sign and persist.** The raw transaction and its hash are written to `tx_outbox` and committed
   before anything is sent. A process that dies after this has a record of exactly what it was about
   to send, and the hash to look it up by.
2. **Broadcast the stored bytes.** Only then is the raw transaction sent, and a retry sends the same
   bytes again — never a re-signed transaction, and never a new decision (FR-E2). The same bytes
   are the same transaction hash, so a rebroadcast of something already mined is refused by the node
   as a used nonce and cannot execute twice (A13).
3. **Record the broadcast** as `submitted`. Inclusion and confirmation are the indexer's to record.

Duplicate execution is refused by the database, not by a check here (data model principle 4). A
second submission of a signed action finds its live transaction and resumes it rather than signing a
new one, and if two submitters race, the partial unique index on `tx_outbox (signed_action_id)`
refuses the second insert and it, too, resumes the first (A06).

Nonces: the relay key's next nonce lives on the run's lease and is taken in one statement, the
larger of the stored value and the chain's pending count (docs/data_model.md section 3.6), so only
the lease holder can take one. The operator key, used for session creation and abort, takes the
larger of the chain's pending count and one past the highest it has recorded.

Gas (ADR-054): the estimate plus a quarter. When the node predicts a revert, the transaction is
still persisted and broadcast, at a fixed fallback limit: the contract decides, and the revert is
then on chain with a receipt, decoded and shown as an execution failure, rather than a prediction
recorded only in our own database.

Replacement (ADR-050): a transaction the relay or operator signed that is still not included
`replace_after_blocks` after its first broadcast is re-signed at the same nonce, with the same
calldata, both fee caps raised by an eighth and never above the ceiling. The original is marked
`replaced` before its successor is inserted, so the two are never live together. Transactions an
agent signed (its setup approval, ADR-040) cannot be replaced: the relay does not hold that key.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import structlog
from eth_account import Account
from eth_account.typed_transactions.typed_transaction import TypedTransaction
from eth_utils.crypto import keccak
from hexbytes import HexBytes

# pyrlp ships no stubs: the one name used here is an exception class to catch.
from rlp.exceptions import RLPException  # type: ignore[import-untyped]

from api.chain import (
    CallRequest,
    ChainAdapter,
    ExchangeCodec,
    ExecutionRevertedError,
    RpcUnavailableError,
    TransactionRejectedError,
)
from api.config import RelayPolicy
from api.db.enums import ActionKind, ActionStatus, TxKind, TxStatus
from api.db.errors import DuplicateError
from api.db.protocols import Transactions, UnitOfWork
from api.db.records import NewOutboxTx, OutboxRecord
from api.relay.fees import Fees, bumped_fees, gas_limit, initial_fees
from api.relay.signer import TransactionSigner
from negotiation_protocol import Address, Digest

#: The outbox kind that carries each kind of signed action.
TX_KIND_FOR_ACTION: Final = {
    ActionKind.OFFER: TxKind.RECORD_OFFER,
    ActionKind.ACCEPT: TxKind.ACCEPT_AND_SETTLE,
    ActionKind.CLOSE: TxKind.CLOSE_SESSION,
}

#: The partial unique index that allows one live transaction per signed action (data model 3.10).
LIVE_PER_ACTION_INDEX: Final = "uq_tx_outbox_signed_action_id_live"

_EIP1559: Final = 2

_log = structlog.get_logger("api.relay")


class RelayError(Exception):
    """A submission the relay refuses: an unknown action, or a raw transaction it cannot read."""


class Reconciliation(StrEnum):
    """What recovery found for one transaction (docs/architecture.md section 5.4)."""

    #: A receipt exists. The indexer records the inclusion; nothing is sent.
    MINED = "mined"
    #: The node holds it and will mine it. Nothing is sent.
    IN_POOL = "in_pool"
    #: Neither mined nor pooled, and its nonce is still free: the stored bytes were sent again.
    REBROADCAST = "rebroadcast"
    #: Another transaction at the same nonce, one this outbox recorded, was mined instead.
    SUPERSEDED = "superseded"
    #: The nonce was consumed by a transaction the outbox does not know: `RECOVERY_REQUIRED`.
    NONCE_CONFLICT = "nonce_conflict"
    #: The RPC did not answer, so nothing is known. An unresolved outage is not an outcome.
    UNREACHABLE = "unreachable"
    #: Neither mined nor pooled, its nonce free, and the node refused the stored bytes — out of gas
    #: money, a wrong chain. `detail` and the row's `last_error` say why, and it is logged
    #: (ADR-057). Sending them again will not help; a person has to.
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    outbox_id: uuid.UUID
    tx_hash: Digest
    outcome: Reconciliation
    detail: str | None = None


class _Sent(StrEnum):
    ACCEPTED = "accepted"
    UNREACHABLE = "unreachable"
    REFUSED = "refused"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def decode_raw_transaction(raw_tx: bytes) -> dict[str, Any]:
    """An EIP-1559 raw transaction's fields, without its signature, ready to re-sign."""
    if not raw_tx or raw_tx[0] != _EIP1559:
        raise RelayError("only EIP-1559 (type 2) transactions are relayed")
    try:
        fields = TypedTransaction.from_bytes(HexBytes(raw_tx)).as_dict()
    except (ValueError, TypeError, RLPException) as error:
        raise RelayError(
            f"the bytes are not a readable transaction: {type(error).__name__}"
        ) from None
    to = fields.get("to")
    return {
        "type": _EIP1559,
        "chainId": int(fields["chainId"]),
        "nonce": int(fields["nonce"]),
        "to": Address("0x" + bytes(to).hex()) if to else None,
        "value": int(fields["value"]),
        "data": bytes(fields["data"]),
        "gas": int(fields["gas"]),
        "maxFeePerGas": int(fields["maxFeePerGas"]),
        "maxPriorityFeePerGas": int(fields["maxPriorityFeePerGas"]),
        "accessList": list(fields.get("accessList", ())),
    }


class Relay:
    def __init__(
        self,
        transactions: Transactions,
        chain: ChainAdapter,
        codec: ExchangeCodec,
        *,
        chain_id: int,
        relay_signer: TransactionSigner,
        holder: str,
        policy: RelayPolicy,
        operator_signer: TransactionSigner | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._transactions = transactions
        self._chain = chain
        self._codec = codec
        self._chain_id = chain_id
        self._relay = relay_signer
        self._operator = operator_signer
        self._holder = holder
        self._policy = policy
        self._clock = clock

    @property
    def relay_address(self) -> Address:
        return self._relay.address

    # -----------------------------------------------------------------------------------------
    # Submission
    # -----------------------------------------------------------------------------------------

    async def submit_action(self, run_id: uuid.UUID, signed_action_id: uuid.UUID) -> OutboxRecord:
        """Relay one participant-signed action. One action is signed into one nonce, once.

        A second call never signs again: it resumes the action's newest transaction — sending its
        stored bytes if they were never accepted — or, when none is live any more (`replaced`,
        `dropped`, `reverted`), returns it for recovery and the indexer to settle. The stage 2.3
        review found the second transaction this rule prevents: a successor recovery had dropped
        as superseded, then a retried turn signing the action afresh.
        """
        async with self._transactions.unit_of_work() as uow:
            action = await uow.signed_actions.get(signed_action_id)
            history = await uow.outbox.for_signed_action(signed_action_id)
        if action is None or action.run_id != run_id:
            raise RelayError(f"run {run_id} has no signed action {signed_action_id}")
        if history:
            return await self._resume(history[-1])

        data = self._codec.encode_signed_action(action.kind, action.typed_message, action.signature)
        try:
            row = await self._persist(
                run_id,
                TX_KIND_FOR_ACTION[action.kind],
                self._relay,
                self._codec.exchange,
                data,
                signed_action_id=signed_action_id,
            )
        except DuplicateError as error:
            if error.constraint != LIVE_PER_ACTION_INDEX:
                raise
            # Another submitter persisted this action's transaction first. Resume theirs (A06).
            async with self._transactions.unit_of_work() as uow:
                live = await uow.outbox.live_for_signed_action(signed_action_id)
            if live is None:
                raise
            return await self._resume(live)
        return await self._broadcast(row)

    async def submit_call(
        self,
        run_id: uuid.UUID,
        kind: TxKind,
        to: Address,
        data: bytes,
        *,
        as_operator: bool = False,
    ) -> OutboxRecord:
        """A lifecycle transaction: `expireSession` from the relay; create, abort or mint from the
        operator. The caller builds the calldata with `ExchangeCodec`."""
        signer = self._relay
        if as_operator:
            if self._operator is None:
                raise RelayError("this relay was given no operator key")
            signer = self._operator
        row = await self._persist(run_id, kind, signer, Address(to), data)
        return await self._broadcast(row)

    async def submit_presigned(
        self, run_id: uuid.UUID, kind: TxKind, raw_tx: bytes
    ) -> OutboxRecord:
        """A transaction someone else signed — an agent's setup approval (ADR-040).

        The sender, nonce and hash are read from the bytes themselves, never taken from the
        caller. Idempotent by hash.
        """
        tx_hash = Digest(keccak(raw_tx))
        fields = decode_raw_transaction(raw_tx)
        if fields["chainId"] != self._chain_id:
            _log.warning("relay.presigned_refused", run_id=str(run_id), reason="wrong chain id")
            raise RelayError(f"the transaction is for chain {fields['chainId']}, not this one")
        sender = Address(Account.recover_transaction(raw_tx))
        async with self._transactions.unit_of_work() as uow:
            existing = await uow.outbox.get_by_hash(tx_hash)
            if existing is None:
                existing = await uow.outbox.add(
                    NewOutboxTx(
                        run_id=run_id,
                        kind=kind,
                        sender=sender,
                        nonce=fields["nonce"],
                        raw_tx=raw_tx,
                        tx_hash=tx_hash,
                    )
                )
        return await self._resume(existing)

    async def _persist(
        self,
        run_id: uuid.UUID,
        kind: TxKind,
        signer: TransactionSigner,
        to: Address,
        data: bytes,
        *,
        signed_action_id: uuid.UUID | None = None,
    ) -> OutboxRecord:
        fees = initial_fees(await self._chain.fee_quote(), self._policy)
        try:
            estimate: int | None = await self._chain.estimate_gas(
                CallRequest(sender=signer.address, to=to, data=data)
            )
        except ExecutionRevertedError:
            estimate = None  # broadcast anyway; the contract decides (ADR-054)
        chain_nonce = await self._chain.pending_nonce(signer.address)

        async with self._transactions.unit_of_work() as uow:
            nonce = await self._take_nonce(uow, run_id, signer, chain_nonce)
            signed = signer.sign_transaction(
                {
                    "type": _EIP1559,
                    "chainId": self._chain_id,
                    "nonce": nonce,
                    "to": to,
                    "value": 0,
                    "data": data,
                    "gas": gas_limit(estimate, self._policy),
                    "maxFeePerGas": fees.max_fee_per_gas,
                    "maxPriorityFeePerGas": fees.max_priority_fee_per_gas,
                }
            )
            return await uow.outbox.add(
                NewOutboxTx(
                    run_id=run_id,
                    kind=kind,
                    sender=signer.address,
                    nonce=nonce,
                    raw_tx=signed.raw_transaction,
                    tx_hash=signed.tx_hash,
                    signed_action_id=signed_action_id,
                )
            )

    async def _take_nonce(
        self, uow: UnitOfWork, run_id: uuid.UUID, signer: TransactionSigner, chain_nonce: int
    ) -> int:
        if signer.address == self._relay.address:
            return await uow.leases.take_relay_nonce(run_id, self._holder, chain_nonce)
        recorded = await uow.outbox.max_nonce(signer.address)
        return chain_nonce if recorded is None else max(chain_nonce, recorded + 1)

    # -----------------------------------------------------------------------------------------
    # Broadcast
    # -----------------------------------------------------------------------------------------

    async def _resume(self, row: OutboxRecord) -> OutboxRecord:
        """A transaction already persisted: send its stored bytes if it was never accepted."""
        if row.status == TxStatus.PENDING:
            return await self._broadcast(row)
        return row

    async def _broadcast(self, row: OutboxRecord) -> OutboxRecord:
        updated, _, _ = await self._send(row)
        return updated

    async def _send(self, row: OutboxRecord) -> tuple[OutboxRecord, _Sent, str | None]:
        """Send the stored bytes and say what came of it — accepted, unanswered or refused — so
        that recovery never reports a resend that did not happen (stage 2.3 review)."""
        try:
            head = await self._chain.head()
            await self._chain.send_raw(row.raw_tx)
        except TransactionRejectedError as error:
            try:
                sent = await self._already_sent(row, error)
            except RpcUnavailableError as unreachable:
                return await self._record_attempt(row, str(unreachable)), _Sent.UNREACHABLE, None
            if not sent:
                _log.warning(
                    "relay.broadcast_refused",
                    run_id=str(row.run_id),
                    outbox_id=str(row.id),
                    tx_hash=str(row.tx_hash),
                    reason=error.reason,
                )
                return await self._record_attempt(row, error.reason), _Sent.REFUSED, error.reason
        except RpcUnavailableError as error:
            return await self._record_attempt(row, str(error)), _Sent.UNREACHABLE, None
        return await self._record_broadcast(row, head.number), _Sent.ACCEPTED, None

    async def _record_broadcast(self, row: OutboxRecord, head_block: int) -> OutboxRecord:
        """The node holds these bytes: `submitted`, and the action it carries with it."""
        async with self._transactions.unit_of_work() as uow:
            updated = await uow.outbox.mark_submitted(row.id, self._clock(), head_block)
            if row.signed_action_id is not None:
                action = await uow.signed_actions.get(row.signed_action_id)
                if action is not None and action.status == ActionStatus.SIGNED:
                    await uow.signed_actions.update_status(action.id, ActionStatus.SUBMITTED)
        return updated

    async def _already_sent(self, row: OutboxRecord, error: TransactionRejectedError) -> bool:
        """A refusal that means these exact bytes already reached the chain or the pool."""
        if error.already_known:
            return True
        return error.nonce_too_low and await self._chain.receipt(row.tx_hash) is not None

    async def _record_attempt(self, row: OutboxRecord, reason: str) -> OutboxRecord:
        async with self._transactions.unit_of_work() as uow:
            return await uow.outbox.record_attempt(row.id, reason)

    # -----------------------------------------------------------------------------------------
    # Recovery (docs/architecture.md section 5.4)
    # -----------------------------------------------------------------------------------------

    async def reconcile(self, run_id: uuid.UUID) -> list[ReconcileResult]:
        """Every unfinished transaction of the run, looked up before anything is sent again."""
        async with self._transactions.unit_of_work() as uow:
            rows = [
                row
                for row in await uow.outbox.unfinished(run_id)
                if row.status in (TxStatus.PENDING, TxStatus.SUBMITTED)
            ]
        results = []
        for row in rows:
            outcome, detail = await self._reconcile_one(row)
            results.append(ReconcileResult(row.id, row.tx_hash, outcome, detail))
        return results

    async def _reconcile_one(self, row: OutboxRecord) -> tuple[Reconciliation, str | None]:
        try:
            if await self._chain.receipt(row.tx_hash) is not None:
                return Reconciliation.MINED, None
            if await self._chain.transaction(row.tx_hash) is not None:
                if row.status == TxStatus.PENDING:
                    await self._mark_submitted_without_sending(row)
                return Reconciliation.IN_POOL, None
            if await self._chain.mined_nonce(row.sender) > row.nonce:
                return await self._nonce_consumed(row), None
        except RpcUnavailableError:
            return Reconciliation.UNREACHABLE, None
        _, sent, reason = await self._send(row)
        if sent == _Sent.UNREACHABLE:
            return Reconciliation.UNREACHABLE, None
        if sent == _Sent.REFUSED:
            return Reconciliation.REFUSED, reason
        return Reconciliation.REBROADCAST, None

    async def _mark_submitted_without_sending(self, row: OutboxRecord) -> None:
        await self._record_broadcast(row, (await self._chain.head()).number)

    async def _nonce_consumed(self, row: OutboxRecord) -> Reconciliation:
        async with self._transactions.unit_of_work() as uow:
            group = await uow.outbox.nonce_group(row.sender, row.nonce)
        for sibling in group:
            if sibling.id != row.id and await self._chain.receipt(sibling.tx_hash) is not None:
                async with self._transactions.unit_of_work() as uow:
                    await uow.outbox.update_status(row.id, TxStatus.DROPPED)
                return Reconciliation.SUPERSEDED
        return Reconciliation.NONCE_CONFLICT

    # -----------------------------------------------------------------------------------------
    # Gas replacement (ADR-050)
    # -----------------------------------------------------------------------------------------

    async def replace_stuck(self, run_id: uuid.UUID) -> list[OutboxRecord]:
        """Re-sign, at a higher fee, each of this relay's transactions stuck past the trigger."""
        head = await self._chain.head()
        async with self._transactions.unit_of_work() as uow:
            stuck = [
                row
                for row in await uow.outbox.unfinished(run_id)
                if row.status == TxStatus.SUBMITTED
                and row.block_number is None
                and row.submitted_block is not None
                and head.number - row.submitted_block >= self._policy.replace_after_blocks
            ]
        successors = []
        for row in stuck:
            successor = await self._replace(row)
            if successor is not None:
                successors.append(successor)
        return successors

    def _signer_for(self, sender: Address) -> TransactionSigner | None:
        for signer in (self._relay, self._operator):
            if signer is not None and signer.address == sender:
                return signer
        return None

    async def _replace(self, row: OutboxRecord) -> OutboxRecord | None:
        signer = self._signer_for(row.sender)
        if signer is None or await self._chain.receipt(row.tx_hash) is not None:
            return None
        fields = decode_raw_transaction(row.raw_tx)
        fees = bumped_fees(
            Fees(fields["maxFeePerGas"], fields["maxPriorityFeePerGas"]), self._policy
        )
        if fees is None:
            return None  # at the ceiling: keep waiting (ADR-050)
        signed = signer.sign_transaction(
            {
                **fields,
                "maxFeePerGas": fees.max_fee_per_gas,
                "maxPriorityFeePerGas": fees.max_priority_fee_per_gas,
            }
        )
        async with self._transactions.unit_of_work() as uow:
            await uow.outbox.update_status(row.id, TxStatus.REPLACED)
            successor = await uow.outbox.add(
                NewOutboxTx(
                    run_id=row.run_id,
                    kind=row.kind,
                    sender=row.sender,
                    nonce=row.nonce,
                    raw_tx=signed.raw_transaction,
                    tx_hash=signed.tx_hash,
                    signed_action_id=row.signed_action_id,
                    replaces_id=row.id,
                )
            )
        return await self._broadcast(successor)
