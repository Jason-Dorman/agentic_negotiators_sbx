#!/usr/bin/env python3
"""Rebuild a negotiation from chain data and a deployment manifest, and nothing else.

    uv run --extra tools python packages/protocol/tools/reconstruct.py \\
        --rpc-url http://127.0.0.1:8545 \\
        --manifest docs/deployments/local-2026-09-24-01.json \\
        --session-id 0x… [--json out.json]

Acceptance A15, following the procedure in docs/protocol.md section 14. The tool reads the chain
and the manifest. It does not read the application database, the export, or any run record — that
is the whole point. A settlement is an economic fact only if it can be established this way, and
the export is believable only where this tool agrees with it (`metrics.audit_complete`).

**What it verifies, and why each step is here**

1. `SessionOpened` exists, and its `configHash` equals the section 3 recomputation over the event's
   own fields. If they differ, the session on-chain is not the session anyone signed for.
2. Every event for the session, in canonical block order.
3. For each signed action: the calldata is decoded, the EIP-712 digest recomputed from the decoded
   struct, and the signer recovered from the signature — then compared with the address the *event*
   names. The contract already made this check; redoing it from calldata is what makes the chain
   self-describing rather than requiring trust in the contract's own claim.
4. The sequence chain is strictly increasing and contiguous from 1. A gap would mean an action was
   recorded that the reconstruction cannot see.
5. The settlement transaction's two ERC-20 `Transfer` logs equal the accepted offer's `quoteAmount`
   and the session's `baseAmount`. Not the event's own amounts — the tokens' logs, which are what
   actually moved.
6. Balances before the session opened and after the terminal event, and the deltas between them.

`web3` is an optional extra of this package (ADR-035): the agent service installs
`negotiation-protocol` and must not get an RPC client, so anything that reads a chain lives behind
`negotiation-protocol[tools]`.

Exit status is 0 when every check passed and 1 when any did not, so it works as a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from negotiation_protocol import (
    Accept,
    Close,
    Domain,
    Offer,
    ProtocolValueError,
    SessionConfig,
    abort_reason_name,
    close_reason_name,
    load_abi,
    recover_signer,
    signature_is_canonical,
    to_bytes32,
    to_hex32,
    validate,
)

try:
    from web3 import Web3
    from web3.exceptions import Web3Exception
    from web3.logs import DISCARD
except (
    ModuleNotFoundError
) as exc:  # pragma: no cover  reason: exercised by installing without the extra
    raise SystemExit(
        "reconstruct.py needs web3, which is an optional extra of this package so that the agent "
        "service cannot acquire an RPC client by depending on the protocol (ADR-035).\n"
        "  Install: uv sync --all-extras   or   pip install 'negotiation-protocol[tools]'"
    ) from exc

#: The seven protocol events (docs/protocol.md section 9), in the order the timeline reads.
SESSION_EVENTS = (
    "OfferRecorded",
    "AcceptanceRecorded",
    "SettlementCompleted",
    "SessionClosed",
    "SessionExpired",
    "SessionAborted",
)

#: Events that carry a participant signature and therefore consume a sequence (section 5).
SIGNED_EVENTS = ("OfferRecorded", "AcceptanceRecorded", "SessionClosed")

#: Which terminal event means which economic outcome (docs/data_model.md section 5).
TERMINAL_OUTCOMES = {
    "SettlementCompleted": "settled",
    "SessionClosed": "closed",
    "SessionExpired": "expired",
    "SessionAborted": "aborted",
}


@dataclass
class Check:
    """One verifiable claim, with the evidence for it."""

    check: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "ok": self.ok, "detail": self.detail}


@dataclass
class Reconstruction:
    session_id: str
    checks: list[Check] = field(default_factory=list)
    document: dict[str, Any] = field(default_factory=dict)

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, detail))
        return ok

    @property
    def ok(self) -> bool:
        """True only when checks ran *and* all of them passed.

        `all([])` is True, so without the first clause a reconstruction that recorded nothing —
        because the tool crashed early, or because a future edit stopped calling the check
        methods — would report itself as successful. "The chain does not support this settlement"
        and "the tool never looked" must not share an answer.
        """
        return bool(self.checks) and all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reconstruction_version": "1",
            "reconstructed_at": datetime.now(UTC).isoformat(),
            "session_id": self.session_id,
            **self.document,
            "checks": [check.as_dict() for check in self.checks],
            "ok": self.ok,
        }


class Reconstructor:
    """Reads one session out of a chain. Holds no state from anywhere else."""

    def __init__(self, w3: Web3, manifest: dict[str, Any]) -> None:
        self._w3 = w3
        self._manifest = manifest
        self._exchange = w3.eth.contract(
            address=Web3.to_checksum_address(manifest["exchange_address"]),
            abi=load_abi("NegotiationExchange"),
        )
        self._base_token = w3.eth.contract(
            address=Web3.to_checksum_address(manifest["base_token_address"]),
            abi=load_abi("MockERC20"),
        )
        self._quote_token = w3.eth.contract(
            address=Web3.to_checksum_address(manifest["quote_token_address"]),
            abi=load_abi("MockERC20"),
        )
        self._domain = Domain(
            chain_id=int(manifest["chain_id"]),
            verifying_contract=manifest["exchange_address"],
        )
        # A lower bound on where this deployment's events can be, per the manifest schema.
        self._start_block = int(manifest["start_block"])

    # -----------------------------------------------------------------------------------
    # Steps 1 to 6 of docs/protocol.md section 14
    # -----------------------------------------------------------------------------------

    def run(self, session_id: str) -> Reconstruction:
        result = Reconstruction(session_id=session_id)
        digest_id = to_bytes32(session_id)

        self._check_chain_identity(result)

        opened = self._session_opened(digest_id)
        if opened is None:
            result.record("session_opened", False, "no SessionOpened event for this session id")
            return result
        result.record("session_opened", True, f"block {opened['blockNumber']}")

        config = self._config_from_event(opened)
        result.document["config"] = self._config_document(opened)
        self._check_config_hash(result, opened, config)

        events = self._session_events(digest_id)
        result.document["event_count"] = len(events)

        actions = [
            self._reconstruct_action(event) for event in events if event["event"] in SIGNED_EVENTS
        ]
        result.document["actions"] = actions
        result.document["events"] = [self._event_document(event) for event in events]

        self._check_signatures(result, actions)
        self._check_sequence_chain(result, actions)

        terminal = self._terminal_event(events)
        result.document["outcome"] = self._outcome_document(terminal)
        result.record(
            "terminal_event",
            terminal is not None,
            "none: the session is still open or expired without being recorded"
            if terminal is None
            else f"{terminal['event']} in block {terminal['blockNumber']}",
        )

        settlement = next(
            (event for event in events if event["event"] == "SettlementCompleted"), None
        )
        if settlement is not None:
            self._check_settlement_transfers(result, settlement, config)

        self._check_balances(result, opened, terminal, config)
        return result

    def _check_chain_identity(self, result: Reconstruction) -> None:
        """A17's automated half: the chain must be the one the manifest describes."""
        chain_id = self._w3.eth.chain_id
        result.record(
            "chain_id",
            chain_id == self._domain.chain_id,
            f"rpc reports {chain_id}, manifest says {self._domain.chain_id}",
        )

        for name, contract, expected in (
            ("exchange", self._exchange, self._manifest["code_hashes"]["exchange"]),
            ("base_token", self._base_token, self._manifest["code_hashes"]["base_token"]),
            ("quote_token", self._quote_token, self._manifest["code_hashes"]["quote_token"]),
        ):
            code = self._w3.eth.get_code(contract.address)
            actual = to_hex32(Web3.keccak(code))
            result.record(
                f"{name}_code_hash",
                actual == expected,
                f"on-chain {actual}, manifest {expected}",
            )

    def _session_opened(self, session_id: bytes) -> dict[str, Any] | None:
        logs = self._exchange.events.SessionOpened().get_logs(
            from_block=self._start_block, argument_filters={"sessionId": session_id}
        )
        return dict(logs[0]) if logs else None

    def _config_from_event(self, opened: dict[str, Any]) -> SessionConfig:
        args = opened["args"]
        return SessionConfig(
            session_id=to_bytes32(args["sessionId"]),
            buyer=args["buyer"],
            seller=args["seller"],
            base_amount=int(args["baseAmount"]),
            expires_at=int(args["expiresAt"]),
            max_offers=int(args["maxOffers"]),
        )

    def _check_config_hash(
        self, result: Reconstruction, opened: dict[str, Any], config: SessionConfig
    ) -> None:
        """Step 1. Recomputed from the event's own fields, never read back from the contract.

        The two token addresses come from the *manifest*, which is also where the agent services
        got them. A session whose recomputed hash differs from the emitted one is a session the
        signatures do not cover.
        """
        emitted = to_hex32(to_bytes32(opened["args"]["configHash"]))
        recomputed = to_hex32(
            config.config_hash(
                self._manifest["base_token_address"], self._manifest["quote_token_address"]
            )
        )
        result.document["config_hash"] = {
            "emitted": emitted,
            "recomputed": recomputed,
            "matches": emitted == recomputed,
        }
        result.record(
            "config_hash", emitted == recomputed, f"emitted {emitted}, recomputed {recomputed}"
        )

        # The event also names the tokens. If those disagree with the manifest, the recomputation
        # above used the wrong pair and its agreement would be meaningless.
        for label, emitted_address, manifest_key in (
            ("base_token", opened["args"]["baseToken"], "base_token_address"),
            ("quote_token", opened["args"]["quoteToken"], "quote_token_address"),
        ):
            expected = Web3.to_checksum_address(self._manifest[manifest_key])
            result.record(
                f"session_{label}",
                Web3.to_checksum_address(emitted_address) == expected,
                f"event {emitted_address}, manifest {expected}",
            )

    def _session_events(self, session_id: bytes) -> list[dict[str, Any]]:
        """Step 2. Every event for the session, in canonical block order."""
        collected: list[dict[str, Any]] = []
        for name in SESSION_EVENTS:
            logs = self._exchange.events[name]().get_logs(
                from_block=self._start_block, argument_filters={"sessionId": session_id}
            )
            collected.extend(dict(log) for log in logs)
        # Block number then log index: the order the chain put them in, which is the only ordering
        # that is a fact rather than a convention.
        return sorted(collected, key=lambda log: (log["blockNumber"], log["logIndex"]))

    def _reconstruct_action(self, event: dict[str, Any]) -> dict[str, Any]:
        """Step 3. Decode the calldata, recompute the digest, recover the signer.

        The digest is rebuilt from the *decoded struct*, not from the event, so the signature is
        checked against the bytes the transaction actually carried.
        """
        tx = self._w3.eth.get_transaction(event["transactionHash"])
        function, arguments = self._exchange.decode_function_input(tx["input"])
        name = function.fn_name

        signature = arguments["signature"]
        message, digest = self._digest_for(name, arguments)
        recovered = recover_signer(digest, signature)
        event_actor = event["args"].get("proposer") or event["args"].get("actor")

        return {
            "sequence": int(event["args"]["sequence"]),
            "kind": {"recordOffer": "offer", "acceptAndSettle": "accept", "closeSession": "close"}[
                name
            ],
            "event": event["event"],
            "function": name,
            "digest": to_hex32(digest),
            "event_actor": event_actor,
            "recovered_signer": recovered,
            "signer_matches_event": Web3.to_checksum_address(event_actor) == recovered,
            "signature_canonical": signature_is_canonical(signature),
            "signature": "0x" + bytes(signature).hex(),
            "typed_message": message,
            "tx_hash": event["transactionHash"].hex(),
            "block_number": int(event["blockNumber"]),
            "log_index": int(event["logIndex"]),
            "submitted_by": tx["from"],
        }

    def _digest_for(
        self, function_name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes]:
        """Rebuild the typed message from decoded calldata and recompute its digest.

        The struct is read by **field name**, exactly as the ABI declares it. That is deliberate:
        reading it positionally would survive a reordering of the struct's members, and a reordered
        struct is a different protocol whose digests silently differ (docs/protocol.md section 15).
        """
        if function_name == "recordOffer":
            raw = arguments["offer"]
            offer = Offer(
                session_id=to_bytes32(raw["sessionId"]),
                config_hash=to_bytes32(raw["configHash"]),
                sequence=int(raw["sequence"]),
                proposer=raw["proposer"],
                quote_amount=int(raw["quoteAmount"]),
                valid_until=int(raw["validUntil"]),
            )
            message = {
                "sessionId": to_hex32(offer.session_id),
                "configHash": to_hex32(offer.config_hash),
                "sequence": offer.sequence,
                "proposer": offer.proposer,
                "quoteAmount": str(offer.quote_amount),
                "validUntil": offer.valid_until,
            }
            return message, offer.digest(self._domain)

        if function_name == "acceptAndSettle":
            raw = arguments["acceptance"]
            acceptance = Accept(
                session_id=to_bytes32(raw["sessionId"]),
                config_hash=to_bytes32(raw["configHash"]),
                sequence=int(raw["sequence"]),
                actor=raw["actor"],
                offer_hash=to_bytes32(raw["offerHash"]),
            )
            message = {
                "sessionId": to_hex32(acceptance.session_id),
                "configHash": to_hex32(acceptance.config_hash),
                "sequence": acceptance.sequence,
                "actor": acceptance.actor,
                "offerHash": to_hex32(acceptance.offer_hash),
            }
            return message, acceptance.digest(self._domain)

        raw = arguments["closure"]
        closure = Close(
            session_id=to_bytes32(raw["sessionId"]),
            config_hash=to_bytes32(raw["configHash"]),
            sequence=int(raw["sequence"]),
            actor=raw["actor"],
            reason=int(raw["reason"]),
        )
        message = {
            "sessionId": to_hex32(closure.session_id),
            "configHash": to_hex32(closure.config_hash),
            "sequence": closure.sequence,
            "actor": closure.actor,
            "reason": closure.reason,
        }
        return message, closure.digest(self._domain)

    def _check_signatures(self, result: Reconstruction, actions: list[dict[str, Any]]) -> None:
        mismatched = [action for action in actions if not action["signer_matches_event"]]
        result.record(
            "signatures_recover_to_the_named_actor",
            not mismatched,
            "all recovered"
            if not mismatched
            else f"{len(mismatched)} mismatch at sequences {[a['sequence'] for a in mismatched]}",
        )

        non_canonical = [action for action in actions if not action["signature_canonical"]]
        result.record(
            "signatures_are_canonical",
            not non_canonical,
            "all low-s"
            if not non_canonical
            else f"high-s at sequences {[a['sequence'] for a in non_canonical]}",
        )

        # The relay pays for gas and signs nothing. A submitter that also appears as a recovered
        # signer would mean the two powers had been combined (docs/protocol.md section 1).
        relay = Web3.to_checksum_address(self._manifest["relay_address"])
        relay_signed = [
            a for a in actions if Web3.to_checksum_address(a["recovered_signer"]) == relay
        ]
        result.record(
            "the_relay_signed_nothing",
            not relay_signed,
            "relay appears only as a submitter"
            if not relay_signed
            else f"relay key signed sequences {[a['sequence'] for a in relay_signed]}",
        )

    def _check_sequence_chain(self, result: Reconstruction, actions: list[dict[str, Any]]) -> None:
        """Step 4. Strictly increasing and contiguous from 1."""
        sequences = [action["sequence"] for action in actions]
        expected = list(range(1, len(sequences) + 1))
        result.document["sequence_chain"] = {
            "sequences": sequences,
            "contiguous": sequences == expected,
        }
        result.record(
            "sequence_chain",
            sequences == expected,
            f"{sequences} against the expected {expected}",
        )

    def _check_settlement_transfers(
        self, result: Reconstruction, settlement: dict[str, Any], config: SessionConfig
    ) -> None:
        """Step 5. The tokens' own `Transfer` logs, not the exchange's claim about them."""
        receipt = self._w3.eth.get_transaction_receipt(settlement["transactionHash"])
        # Filtered by emitting address, not only by event signature. The two mock tokens are two
        # deployments of the same contract, so `process_receipt` on either instance decodes *both*
        # legs — which made this function report four transfers for a two-leg settlement the first
        # time it ran against a chain.
        quote_transfers = [
            log
            for log in self._quote_token.events.Transfer().process_receipt(receipt, errors=DISCARD)
            if log["address"] == self._quote_token.address
        ]
        base_transfers = [
            log
            for log in self._base_token.events.Transfer().process_receipt(receipt, errors=DISCARD)
            if log["address"] == self._base_token.address
        ]

        # The amount that must have moved is the one the *offer was signed for*, recovered from
        # that offer's calldata — not the one the settlement event announces. Comparing the
        # transfers against the event would only prove the contract was internally consistent: a
        # settlement that moved an amount nobody signed for, and emitted that same amount, would
        # have passed every check in this function. The signed amount is what two participants
        # authorised, so it is what the chain has to be held to.
        accepted = to_hex32(to_bytes32(settlement["args"]["offerHash"]))
        offers = [a for a in result.document["actions"] if a["kind"] == "offer"]
        signed_offer = next((o for o in offers if o["digest"] == accepted), None)

        announced_quote = int(settlement["args"]["quoteAmount"])
        signed_quote = (
            int(signed_offer["typed_message"]["quoteAmount"]) if signed_offer is not None else None
        )
        expected_quote = announced_quote if signed_quote is None else signed_quote

        buyer = Web3.to_checksum_address(config.buyer)
        seller = Web3.to_checksum_address(config.seller)

        quote_leg = [
            t
            for t in quote_transfers
            if t["args"]["from"] == buyer
            and t["args"]["to"] == seller
            and int(t["args"]["value"]) == expected_quote
        ]
        base_leg = [
            t
            for t in base_transfers
            if t["args"]["from"] == seller
            and t["args"]["to"] == buyer
            and int(t["args"]["value"]) == config.base_amount
        ]

        result.document["settlement"] = {
            "tx_hash": settlement["transactionHash"].hex(),
            "quote_amount": str(expected_quote),
            "signed_quote_amount": None if signed_quote is None else str(signed_quote),
            "announced_quote_amount": str(announced_quote),
            "base_amount": str(config.base_amount),
            "quote_transfers": len(quote_transfers),
            "base_transfers": len(base_transfers),
        }

        # Reported separately from the transfer legs, because the two failures are different facts
        # about the run: an event that disagrees with the signature it settled means the contract
        # misreported, while a transfer that disagrees with the signature means tokens moved on
        # authority nobody gave.
        result.record(
            "settlement_amount_was_signed",
            signed_quote is not None,
            f"settled digest {accepted} matches a recorded offer signed for {signed_quote}"
            if signed_quote is not None
            else f"settled digest {accepted} matches no recorded offer, so no signed amount exists "
            "to check the transfers against; falling back to the event's own figure",
        )
        result.record(
            "settlement_event_matches_the_signed_amount",
            signed_quote is None or announced_quote == signed_quote,
            f"event announced {announced_quote}, offer was signed for {signed_quote}",
        )
        result.record(
            "settlement_quote_leg",
            len(quote_leg) == 1,
            f"{len(quote_leg)} transfer(s) of {expected_quote} quote from buyer to seller",
        )
        result.record(
            "settlement_base_leg",
            len(base_leg) == 1,
            f"{len(base_leg)} transfer(s) of {config.base_amount} base from seller to buyer",
        )
        # Exactly one transfer per token: no fee leg, no third party, no mint, no second payment.
        result.record(
            "settlement_moved_nothing_else",
            len(quote_transfers) == 1 and len(base_transfers) == 1,
            f"{len(quote_transfers)} quote and {len(base_transfers)} base transfer(s) in the tx",
        )

        # And nothing at all from an address the manifest does not name. A settlement that also
        # touched a fourth contract would satisfy every check above; this is what makes "the
        # exchange has no arbitrary token or target parameters" (docs/protocol.md section 8) an
        # observation about the transaction rather than a claim about the source.
        known = {self._exchange.address, self._base_token.address, self._quote_token.address}
        strangers = sorted({log["address"] for log in receipt["logs"]} - known)
        result.record(
            "settlement_touched_only_manifest_contracts",
            not strangers,
            "only the exchange and its token pair emitted logs"
            if not strangers
            else f"unexpected log emitters: {strangers}",
        )

        # The accepted digest must be the one a preceding OfferRecorded carried.
        result.record(
            "settled_digest_was_a_recorded_offer",
            signed_offer is not None,
            f"settled digest {accepted} against {len(offers)} recorded offer(s)",
        )

    def _check_balances(
        self,
        result: Reconstruction,
        opened: dict[str, Any],
        terminal: dict[str, Any] | None,
        config: SessionConfig,
    ) -> None:
        """Step 6. Balances at the block before the session opened and after the terminal event."""
        before = max(int(opened["blockNumber"]) - 1, 0)
        after = int(terminal["blockNumber"]) if terminal else int(self._w3.eth.block_number)

        balances: dict[str, Any] = {"before_block": before, "after_block": after}
        deltas: dict[str, dict[str, str]] = {}

        for role, address in (("buyer", config.buyer), ("seller", config.seller)):
            checksummed = Web3.to_checksum_address(address)
            row: dict[str, dict[str, str]] = {}
            for token_name, token in (("base", self._base_token), ("quote", self._quote_token)):
                start = int(token.functions.balanceOf(checksummed).call(block_identifier=before))
                end = int(token.functions.balanceOf(checksummed).call(block_identifier=after))
                row[token_name] = {"before": str(start), "after": str(end)}
                deltas.setdefault(role, {})[token_name] = str(end - start)
            balances[role] = row

        balances["deltas"] = deltas
        result.document["balances"] = balances

        if terminal is not None and terminal["event"] == "SettlementCompleted":
            quote = int(terminal["args"]["quoteAmount"])
            expected = {
                "buyer": {"base": str(config.base_amount), "quote": str(-quote)},
                "seller": {"base": str(-config.base_amount), "quote": str(quote)},
            }
            result.record(
                "balance_deltas_match_the_settlement",
                deltas == expected,
                f"observed {deltas}, expected {expected}",
            )
        else:
            unchanged = all(value == "0" for row in deltas.values() for value in row.values())
            # A session that did not settle must have moved nothing. This is the check that makes
            # a no-deal a *result* rather than an absence of evidence (A03).
            result.record(
                "no_settlement_moved_no_tokens",
                unchanged,
                f"observed {deltas}",
            )

    def _terminal_event(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(events):
            if event["event"] in TERMINAL_OUTCOMES:
                return event
        return None

    # -----------------------------------------------------------------------------------
    # Documents
    # -----------------------------------------------------------------------------------

    def _config_document(self, opened: dict[str, Any]) -> dict[str, Any]:
        args = opened["args"]
        return {
            "session_id": to_hex32(to_bytes32(args["sessionId"])),
            "buyer": args["buyer"],
            "seller": args["seller"],
            "base_token": args["baseToken"],
            "quote_token": args["quoteToken"],
            "base_amount": str(args["baseAmount"]),
            "expires_at": int(args["expiresAt"]),
            "max_offers": int(args["maxOffers"]),
            "opened_in_block": int(opened["blockNumber"]),
            "opened_tx_hash": opened["transactionHash"].hex(),
        }

    def _event_document(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "event": event["event"],
            "block_number": int(event["blockNumber"]),
            "block_hash": event["blockHash"].hex(),
            "tx_hash": event["transactionHash"].hex(),
            "log_index": int(event["logIndex"]),
        }

    def _outcome_document(self, terminal: dict[str, Any] | None) -> dict[str, Any]:
        if terminal is None:
            return {"kind": "pending", "terminal_event": None, "reason_code": None, "reason": None}

        kind = TERMINAL_OUTCOMES[terminal["event"]]
        code = terminal["args"].get("reason")
        reason: str | None = None
        if code is not None:
            reason = (
                abort_reason_name(int(code))
                if terminal["event"] == "SessionAborted"
                else close_reason_name(int(code))
            )

        return {
            "kind": kind,
            "terminal_event": terminal["event"],
            "block_number": int(terminal["blockNumber"]),
            "tx_hash": terminal["transactionHash"].hex(),
            "reason_code": None if code is None else int(code),
            "reason": reason,
        }


def render(result: Reconstruction) -> str:
    lines = [f"session {result.session_id}"]
    outcome = result.document.get("outcome", {})
    lines.append(f"outcome  {outcome.get('kind', 'unknown')} ({outcome.get('terminal_event')})")
    for action in result.document.get("actions", []):
        lines.append(
            f"  seq {action['sequence']}  {action['kind']:<7}"
            f" signed by {action['recovered_signer']}"
            f"  submitted by {action['submitted_by']}"
        )
    lines.append("")
    for check in result.checks:
        lines.append(f"  [{'ok  ' if check.ok else 'FAIL'}] {check.check}: {check.detail}")
    lines.append("")
    lines.append("RECONSTRUCTED" if result.ok else "RECONSTRUCTION FAILED")
    return "\n".join(lines)


#: Exit statuses, and the reason there are three of them. A gate has to be able to tell "the chain
#: does not support this settlement" from "the tool never got far enough to look" — and an uncaught
#: traceback exits 1, which is the first of those. Every way of not looking returns 2 instead.
EXIT_RECONSTRUCTED = 0
EXIT_CHECK_FAILED = 1
EXIT_CANNOT_RUN = 2


def load_manifest(path: Path) -> dict[str, Any]:
    """Read and validate a manifest, raising `CannotRunError` with a readable message.

    Validated against its own schema rather than trusted, because the failure this prevents is
    silent: a manifest missing `code_hashes` would make the tool skip the identity checks it exists
    to perform, and a missing `start_block` would make it scan from the wrong place.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CannotRunError(f"cannot read the manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CannotRunError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise CannotRunError(f"{path} is valid JSON but not an object")

    try:
        validate(document, "deployment_manifest.v1.json")
    except ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        raise CannotRunError(
            f"{path} does not match deployment_manifest.v1.json at {location}: {exc.message}"
        ) from exc

    manifest: dict[str, Any] = document
    return manifest


class CannotRunError(Exception):
    """The tool cannot reach the point of making a check. Exit status 2, never 1."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--session-id", required=True, help="0x + 64 hex")
    parser.add_argument("--json", type=Path, help="write the reconstruction document here")
    args = parser.parse_args(argv)

    try:
        try:
            to_bytes32(args.session_id)
        except (ProtocolValueError, ValueError) as exc:
            raise CannotRunError(
                f"--session-id must be 0x followed by 64 hex digits: {exc}"
            ) from exc

        manifest = load_manifest(args.manifest)

        w3 = Web3(Web3.HTTPProvider(args.rpc_url))
        try:
            reachable = w3.is_connected()
        except Exception as exc:
            raise CannotRunError(f"cannot reach {args.rpc_url}: {exc}") from exc
        if not reachable:
            raise CannotRunError(f"cannot reach {args.rpc_url}")

        try:
            result = Reconstructor(w3, manifest).run(args.session_id)
        except (Web3Exception, ProtocolValueError, OSError) as exc:
            raise CannotRunError(f"reading the chain failed: {type(exc).__name__}: {exc}") from exc
    except CannotRunError as exc:
        print(f"cannot reconstruct: {exc}", file=sys.stderr)
        print(
            "  Exit status 2 means the tool did not reach a verdict. It is not a failed check.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    if args.json:
        args.json.write_text(json.dumps(result.as_dict(), indent=2) + "\n", encoding="utf-8")
    print(render(result))
    return EXIT_RECONSTRUCTED if result.ok else EXIT_CHECK_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
