"""The exported ABI against docs/protocol.md sections 8 and 9.

The ABI is the indexer's and the reconstruction tool's only means of decoding an event or a
revert, so an event whose field order differs from the document decodes into the wrong values
rather than failing. Every event signature and every error name is asserted literally here,
against the text of the protocol document rather than against the contract, because the contract
is what is under test.

`INegotiationExchange.sol` restates the same declarations in Solidity, and the Foundry suite
checks the contract against them. This module closes the loop from the other side: three
independent statements of the same seven events, and any two disagreeing fails a build.
"""

from __future__ import annotations

from typing import Any

import pytest

from negotiation_protocol import load_abi

EXCHANGE_ABI = load_abi("NegotiationExchange")
TOKEN_ABI = load_abi("MockERC20")

#: docs/protocol.md section 9, in order. `indexed` is part of the contract with an observer: an
#: indexed field is filterable by an explorer and a log query, a non-indexed one is not.
EXPECTED_EVENTS: dict[str, list[tuple[str, str, bool]]] = {
    "SessionOpened": [
        ("sessionId", "bytes32", True),
        ("buyer", "address", True),
        ("seller", "address", True),
        ("baseToken", "address", False),
        ("quoteToken", "address", False),
        ("baseAmount", "uint256", False),
        ("expiresAt", "uint64", False),
        ("maxOffers", "uint16", False),
        ("configHash", "bytes32", False),
    ],
    "OfferRecorded": [
        ("sessionId", "bytes32", True),
        ("sequence", "uint64", False),
        ("proposer", "address", True),
        ("quoteAmount", "uint256", False),
        ("validUntil", "uint64", False),
        ("offerHash", "bytes32", False),
    ],
    "AcceptanceRecorded": [
        ("sessionId", "bytes32", True),
        ("sequence", "uint64", False),
        ("actor", "address", True),
        ("offerHash", "bytes32", False),
    ],
    "SettlementCompleted": [
        ("sessionId", "bytes32", True),
        ("buyer", "address", False),
        ("seller", "address", False),
        ("baseToken", "address", False),
        ("baseAmount", "uint256", False),
        ("quoteToken", "address", False),
        ("quoteAmount", "uint256", False),
        ("offerHash", "bytes32", False),
    ],
    "SessionClosed": [
        ("sessionId", "bytes32", True),
        ("sequence", "uint64", False),
        ("actor", "address", True),
        ("reason", "uint8", False),
    ],
    "SessionExpired": [("sessionId", "bytes32", True), ("expiresAt", "uint64", False)],
    "SessionAborted": [
        ("sessionId", "bytes32", True),
        ("operator", "address", True),
        ("reason", "uint8", False),
    ],
}

#: docs/protocol.md section 8.3, plus `InvalidTokenPair` (Q17, ADR-037).
EXPECTED_ERRORS: dict[str, list[str]] = {
    "NotOperator": [],
    "SessionExists": [],
    "InvalidParties": [],
    "InvalidTokenPair": [],
    "InvalidBaseAmount": [],
    "InvalidExpiry": [],
    "InvalidMaxOffers": [],
    "SessionNotOpen": ["uint8"],
    "SessionDeadlinePassed": [],
    "SessionNotExpired": [],
    "ConfigHashMismatch": [],
    "SequenceMismatch": ["uint64", "uint64"],
    "BadSignature": [],
    "NotParticipant": ["address"],
    "NotProposerTurn": ["address"],
    "OfferLimitReached": ["uint16"],
    "InvalidQuoteAmount": [],
    "InvalidValidUntil": [],
    "NoActiveOffer": [],
    "StaleOfferDigest": ["bytes32", "bytes32"],
    "OfferExpired": [],
    "SelfAcceptance": [],
    "InvalidReason": ["uint8"],
}

#: Declarations the ABI carries from OpenZeppelin rather than from docs/protocol.md. They are
#: listed rather than tolerated by a loose assertion, because the indexer has to decode them too:
#: a `SafeERC20FailedOperation` revert is a real settlement failure and must be reported as an
#: execution failure rather than as an unknown selector (docs/api_contract.md section 4).
#:
#: `EIP712DomainChanged` can never be emitted here — the domain is fixed at construction and
#: nothing calls the internal that emits it — but it is part of ERC-5267 and so part of the ABI.
INHERITED_EVENTS = frozenset({"EIP712DomainChanged"})
INHERITED_FUNCTIONS = frozenset({"eip712Domain"})  # ERC-5267
INHERITED_ERRORS = frozenset(
    {
        "ReentrancyGuardReentrantCall",  # ReentrancyGuard, on a re-entered acceptAndSettle
        "SafeERC20FailedOperation",  # SafeERC20, when a transfer leg fails (A10)
        "InvalidShortString",  # ShortStrings, unreachable: the domain strings are constants
        "StringTooLong",  # ShortStrings, likewise
    }
)

#: docs/protocol.md section 8. Only the entry points the protocol names; the ABI may also carry
#: inherited views such as `eip712Domain`, which are not the protocol's business.
EXPECTED_FUNCTIONS = (
    "createSession",
    "recordOffer",
    "acceptAndSettle",
    "closeSession",
    "expireSession",
    "abortSession",
    "getSession",
    "hashOffer",
    "hashAccept",
    "hashClose",
    "baseToken",
    "quoteToken",
    "operator",
)


def _by_type(abi: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in abi if entry.get("type") == kind}


class TestEvents:
    @pytest.mark.parametrize("name", sorted(EXPECTED_EVENTS))
    def test_the_event_has_the_documented_fields_in_order(self, name: str) -> None:
        event = _by_type(EXCHANGE_ABI, "event")[name]
        actual = [(item["name"], item["type"], item["indexed"]) for item in event["inputs"]]
        assert actual == EXPECTED_EVENTS[name]

    def test_the_event_set_is_the_seven_plus_the_named_inherited_one(self) -> None:
        # An eighth protocol event would be a fact about a run that the reconstruction procedure
        # in docs/protocol.md section 14 does not collect, so it would be invisible in the
        # evidence. The inherited set is named explicitly so that adding a dependency which
        # brings its own events is a decision rather than a silent widening.
        assert set(_by_type(EXCHANGE_ABI, "event")) == set(EXPECTED_EVENTS) | INHERITED_EVENTS


class TestErrors:
    @pytest.mark.parametrize("name", sorted(EXPECTED_ERRORS))
    def test_the_error_has_the_documented_parameter_types(self, name: str) -> None:
        error = _by_type(EXCHANGE_ABI, "error")[name]
        assert [item["type"] for item in error["inputs"]] == EXPECTED_ERRORS[name]

    def test_the_error_set_is_exactly_the_protocol_table(self) -> None:
        # Errors are part of the protocol (ADR-017): the indexer decodes a revert into a stable
        # code, so an undeclared error surfaces as an unknown selector and a failed action
        # becomes indistinguishable from an infrastructure fault.
        assert set(_by_type(EXCHANGE_ABI, "error")) == set(EXPECTED_ERRORS) | INHERITED_ERRORS

    def test_the_inherited_errors_are_decodable_too(self) -> None:
        # Not a formality. A settlement whose second leg fails reverts with
        # `SafeERC20FailedOperation`, and A10 requires that to be reported as an execution
        # failure with no trade — which the indexer can only do if the selector is in the ABI.
        errors = _by_type(EXCHANGE_ABI, "error")
        assert errors["SafeERC20FailedOperation"]["inputs"][0]["type"] == "address"
        assert errors["ReentrancyGuardReentrantCall"]["inputs"] == []


class TestFunctions:
    @pytest.mark.parametrize("name", EXPECTED_FUNCTIONS)
    def test_the_entry_point_is_present(self, name: str) -> None:
        assert name in _by_type(EXCHANGE_ABI, "function")

    def test_the_function_set_is_exactly_the_protocol_interface(self) -> None:
        """An exact set, the way the events and errors are already asserted.

        The denylist below is a readable statement of intent, but it is only a list of fifteen
        names: an escape hatch called anything else — `emergencyTransfer`, `adminSettle` — passed
        the whole ABI suite. An exact set makes any new external function a deliberate change to
        this test, which is where someone reviewing it would look.
        """
        assert (
            set(_by_type(EXCHANGE_ABI, "function")) == set(EXPECTED_FUNCTIONS) | INHERITED_FUNCTIONS
        )

    def test_there_is_no_upgrade_pause_or_ownership_surface(self) -> None:
        """docs/protocol.md section 8: non-upgradeable, no owner transfer, no pause, no permit.

        Asserted against the ABI rather than trusted from the source, because this is the claim
        an observer is asked to accept about a contract they did not compile.
        """
        forbidden = {
            "upgradeTo",
            "upgradeToAndCall",
            "initialize",
            "transferOwnership",
            "renounceOwnership",
            "owner",
            "pause",
            "unpause",
            "permit",
            "setOperator",
            "setBaseToken",
            "setQuoteToken",
            "rescueTokens",
            "sweep",
            "withdraw",
        }
        assert not forbidden & set(_by_type(EXCHANGE_ABI, "function"))

    def test_the_settlement_entry_point_takes_no_token_or_target_parameter(self) -> None:
        # "No arbitrary token or target parameters" (docs/protocol.md section 8). The token pair
        # is fixed at construction, so the only inputs are the acceptance and its signature.
        accept = _by_type(EXCHANGE_ABI, "function")["acceptAndSettle"]
        assert [item["name"] for item in accept["inputs"]] == ["acceptance", "signature"]


class TestMockToken:
    def test_the_token_mints_and_exposes_its_operator(self) -> None:
        functions = _by_type(TOKEN_ABI, "function")
        assert "mint" in functions
        assert "operator" in functions

    def test_the_token_has_no_burn_or_ownership_transfer(self) -> None:
        # Minting exists only so the operator can fund fresh wallets before a run, and the
        # operator role is immutable (docs/protocol.md section 2).
        functions = set(_by_type(TOKEN_ABI, "function"))
        assert not {"burn", "burnFrom", "transferOwnership", "setOperator"} & functions
