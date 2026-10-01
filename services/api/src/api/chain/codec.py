"""Encode calls to, and decode logs, calldata and reverts from, the three deployed contracts.

Everything is driven by the committed ABI artefacts in `packages/protocol/abi/`, which `make
artefacts` checks against the compiled contracts, so the codec cannot drift from the deployment:
an event, function or error this module knows is one the contract declares, by the same signature.

**A log is decoded only if it came from the address it is decoded for.** Both mock tokens share
one ABI, and web3's `process_receipt` decodes by event signature regardless of who emitted the log,
which in stage 1 made every two-leg settlement appear to have four transfers (docs/contributing.md
section 3). Here an exchange event is decoded only from the manifest's exchange address and a
`Transfer` only from one of its two token addresses.

**Decoded values take their JSON form at once.** `chain_events.decoded` and `calldata.decoded_args`
are JSONB and appear in the export under protocol field names (docs/api_contract.md section 5), so
the conversion happens here, once: a `bytes32` becomes `0x` + 64 lowercase hex, an address is
checksummed, a `uint256` — every one in this protocol is a token amount — becomes a base-10
string, and the narrower integers (`sequence`, `validUntil`, `expiresAt`, `maxOffers`, `reason`)
stay integers, as `typed_message` has them.

**Calls are encoded from the stored typed message by the ABI's own component names.** The typed
message carries the Solidity field names (api_contract section 6), which are exactly the names of
the struct the contract takes, so the relay never maps a field by hand and cannot put one in the
wrong slot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from api.chain.types import RawLog
from negotiation_protocol import (
    Address,
    MinorAmount,
    ProtocolValueError,
    SessionConfig,
    load_abi,
    to_bytes32,
)

#: The seven events of docs/protocol.md section 9, the only names `chain_events.event_name` holds.
EXCHANGE_EVENTS: Final = (
    "SessionOpened",
    "OfferRecorded",
    "AcceptanceRecorded",
    "SettlementCompleted",
    "SessionClosed",
    "SessionExpired",
    "SessionAborted",
)

#: The function that submits each kind of signed action, and its struct parameter's name.
SIGNED_ACTION_FUNCTIONS: Final = {
    "offer": "recordOffer",
    "accept": "acceptAndSettle",
    "close": "closeSession",
}

#: Solidity's two built-in revert shapes, beside the protocol's custom errors.
_ERROR_STRING_SELECTOR: Final = keccak(text="Error(string)")[:4]
_PANIC_SELECTOR: Final = keccak(text="Panic(uint256)")[:4]


class CodecError(ValueError):
    """A value cannot be encoded for, or a log decoded from, the contract's ABI."""


def _canonical_type(parameter: Mapping[str, Any]) -> str:
    """The ABI type as a signature spells it: a tuple is written out as its components."""
    kind: str = parameter["type"]
    if kind.startswith("tuple"):
        inner = ",".join(_canonical_type(component) for component in parameter["components"])
        return f"({inner}){kind[len('tuple') :]}"
    return kind


def _signature(entry: Mapping[str, Any]) -> str:
    return f"{entry['name']}({','.join(_canonical_type(item) for item in entry['inputs'])})"


def _to_json(parameter: Mapping[str, Any], value: Any) -> Any:
    """One decoded ABI value in its JSON form (module docstring)."""
    kind: str = parameter["type"]
    if kind == "tuple":
        components = parameter["components"]
        return {
            component["name"]: _to_json(component, item)
            for component, item in zip(components, value, strict=True)
        }
    if kind == "address":
        return to_checksum_address(value)
    if kind == "bytes32":
        return "0x" + bytes(value).hex()
    if kind == "bytes":
        return "0x" + bytes(value).hex()
    if kind == "uint256":
        return str(int(value))
    if kind.startswith(("uint", "int")):
        return int(value)
    return value


def _from_json(parameter: Mapping[str, Any], value: Any) -> Any:
    """The inverse of `_to_json`, strict: a value that is not exactly its type's form is refused."""
    kind: str = parameter["type"]
    if kind == "tuple":
        if not isinstance(value, Mapping):
            raise CodecError(f"{parameter['name']} must be an object")
        return tuple(
            _from_json(component, value[component["name"]]) for component in parameter["components"]
        )
    if kind == "address":
        return Address(value)
    if kind == "bytes32":
        return to_bytes32(value)
    if kind == "bytes":
        return bytes.fromhex(str(value).removeprefix("0x"))
    if kind.startswith("uint"):
        number = int(MinorAmount.parse(value) if isinstance(value, str) else MinorAmount(value))
        bits = int(kind[len("uint") :] or 256)
        if number >= 1 << bits:
            raise CodecError(f"{parameter['name']} does not fit {kind}")
        return number
    raise CodecError(f"no encoding for ABI type {kind}")


@dataclass(frozen=True, slots=True)
class DecodedEvent:
    """An exchange event: its name, its arguments in JSON form, and the log it came from."""

    name: str
    args: dict[str, Any]
    log: RawLog

    @property
    def session_id(self) -> str:
        return str(self.args["sessionId"])


@dataclass(frozen=True, slots=True)
class Transfer:
    """An ERC-20 `Transfer` from one of the deployment's two tokens."""

    token: Address
    sender: Address
    recipient: Address
    amount: int
    log: RawLog


@dataclass(frozen=True, slots=True)
class DecodedCall:
    """Transaction calldata: `{ to, input, decoded_function, decoded_args }` (data model 3.11)."""

    function: str
    args: dict[str, Any]

    def as_document(self, to: Address, data: bytes) -> dict[str, Any]:
        return {
            "to": str(to),
            "input": "0x" + data.hex(),
            "decoded_function": self.function,
            "decoded_args": self.args,
        }


@dataclass(frozen=True, slots=True)
class DecodedRevert:
    """A revert's error name and arguments. `name` is a protocol error name when it is one."""

    name: str
    args: dict[str, Any]


class _Abi:
    """Lookups over one ABI: events by topic, functions by selector and name, errors by selector."""

    def __init__(self, abi: Sequence[Mapping[str, Any]]) -> None:
        self.events = {
            keccak(text=_signature(entry)): entry for entry in abi if entry["type"] == "event"
        }
        functions = [entry for entry in abi if entry["type"] == "function"]
        self.functions = {keccak(text=_signature(entry))[:4]: entry for entry in functions}
        self.functions_by_name = {entry["name"]: entry for entry in functions}
        self.errors = {
            keccak(text=_signature(entry))[:4]: entry for entry in abi if entry["type"] == "error"
        }

    def encode(self, name: str, args: Sequence[Any]) -> bytes:
        entry = self.functions_by_name[name]
        types = [_canonical_type(item) for item in entry["inputs"]]
        return keccak(text=_signature(entry))[:4] + abi_encode(types, list(args))

    def decode_event(self, log: RawLog) -> tuple[str, dict[str, Any]] | None:
        if not log.topics or log.topics[0] not in self.events:
            return None
        entry = self.events[log.topics[0]]
        indexed = [item for item in entry["inputs"] if item["indexed"]]
        plain = [item for item in entry["inputs"] if not item["indexed"]]
        if len(log.topics) != len(indexed) + 1:
            return None
        values: dict[str, Any] = {}
        for item, topic in zip(indexed, log.topics[1:], strict=True):
            (value,) = abi_decode([_canonical_type(item)], topic)
            values[item["name"]] = _to_json(item, value)
        decoded = abi_decode([_canonical_type(item) for item in plain], log.data)
        for item, value in zip(plain, decoded, strict=True):
            values[item["name"]] = _to_json(item, value)
        # The ABI's declaration order, not "indexed first", so the document reads like the event.
        return entry["name"], {item["name"]: values[item["name"]] for item in entry["inputs"]}


class ExchangeCodec:
    """The deployment's exchange and its two tokens, by address (module docstring)."""

    def __init__(self, exchange: Address, base_token: Address, quote_token: Address) -> None:
        self.exchange = Address(exchange)
        self.base_token = Address(base_token)
        self.quote_token = Address(quote_token)
        self._exchange_abi = _Abi(load_abi("NegotiationExchange"))
        self._token_abi = _Abi(load_abi("MockERC20"))

    # -- logs ----------------------------------------------------------------------------------

    def decode_event(self, log: RawLog) -> DecodedEvent | None:
        """An exchange event, or None for a log the exchange did not emit."""
        if log.address != self.exchange:
            return None
        decoded = self._exchange_abi.decode_event(log)
        # The ABI also declares OpenZeppelin's `EIP712DomainChanged` (EIP-5267), which this
        # contract never emits and the protocol does not list; only the seven are events here.
        if decoded is None or decoded[0] not in EXCHANGE_EVENTS:
            return None
        name, args = decoded
        return DecodedEvent(name, args, log)

    def decode_transfer(self, log: RawLog) -> Transfer | None:
        """A `Transfer` from the base or the quote token, or None for anything else."""
        if log.address not in (self.base_token, self.quote_token):
            return None
        decoded = self._token_abi.decode_event(log)
        if decoded is None or decoded[0] != "Transfer":
            return None
        args = decoded[1]
        return Transfer(
            token=log.address,
            sender=Address(args["from"]),
            recipient=Address(args["to"]),
            amount=int(args["value"]),
            log=log,
        )

    # -- calldata ------------------------------------------------------------------------------

    def decode_call(self, to: Address | None, data: bytes) -> DecodedCall | None:
        """Exchange calldata by its selector, or None for a call to anything else."""
        if to is None or Address(to) != self.exchange or len(data) < 4:
            return None
        entry = self._exchange_abi.functions.get(data[:4])
        if entry is None:
            return None
        values = abi_decode([_canonical_type(item) for item in entry["inputs"]], data[4:])
        args = {
            item["name"]: _to_json(item, value)
            for item, value in zip(entry["inputs"], values, strict=True)
        }
        return DecodedCall(entry["name"], args)

    # -- reverts -------------------------------------------------------------------------------

    def decode_revert(self, data: bytes) -> DecodedRevert:
        """The protocol error a revert carries (docs/protocol.md section 8.3, ADR-017)."""
        if len(data) < 4:
            return DecodedRevert("NoRevertData", {})
        selector, payload = data[:4], data[4:]
        entry = self._exchange_abi.errors.get(selector) or self._token_abi.errors.get(selector)
        if entry is not None:
            values = abi_decode([_canonical_type(item) for item in entry["inputs"]], payload)
            return DecodedRevert(
                entry["name"],
                {
                    item["name"]: _to_json(item, value)
                    for item, value in zip(entry["inputs"], values, strict=True)
                },
            )
        if selector == _ERROR_STRING_SELECTOR:
            (message,) = abi_decode(["string"], payload)
            return DecodedRevert("Error", {"message": str(message)})
        if selector == _PANIC_SELECTOR:
            (code,) = abi_decode(["uint256"], payload)
            return DecodedRevert("Panic", {"code": int(code)})
        return DecodedRevert(f"UnknownError(0x{selector.hex()})", {})

    # -- encoding ------------------------------------------------------------------------------

    def encode_signed_action(
        self, kind: str, typed_message: Mapping[str, Any], signature: str
    ) -> bytes:
        """`recordOffer`, `acceptAndSettle` or `closeSession`, from the stored signed action."""
        function = SIGNED_ACTION_FUNCTIONS.get(kind)
        if function is None:
            raise CodecError(f"no exchange function submits a signed {kind!r}")
        struct = self._exchange_abi.functions_by_name[function]["inputs"][0]
        expected = {component["name"] for component in struct["components"]}
        if set(typed_message) != expected:
            raise CodecError(f"a {kind} message has exactly the fields {sorted(expected)}")
        try:
            values = _from_json(struct, typed_message)
            signature_bytes = bytes.fromhex(signature.removeprefix("0x"))
        except (ProtocolValueError, ValueError, KeyError) as error:
            raise CodecError(f"the stored {kind} cannot be encoded: {error}") from None
        if len(signature_bytes) != 65:
            raise CodecError("a signature is 65 bytes")
        return self._exchange_abi.encode(function, [values, signature_bytes])

    def encode_create_session(self, config: SessionConfig) -> bytes:
        struct = (
            to_bytes32(config.session_id),
            Address(config.buyer),
            Address(config.seller),
            config.base_amount,
            config.expires_at,
            config.max_offers,
        )
        return self._exchange_abi.encode("createSession", [struct])

    def encode_expire_session(self, session_id: str) -> bytes:
        return self._exchange_abi.encode("expireSession", [to_bytes32(session_id)])

    def encode_abort_session(self, session_id: str, reason: int) -> bytes:
        return self._exchange_abi.encode("abortSession", [to_bytes32(session_id), reason])

    def encode_mint(self, to: Address, amount: int) -> bytes:
        return self._token_abi.encode("mint", [Address(to), amount])

    def encode_approve(self, spender: Address, amount: int) -> bytes:
        return self._token_abi.encode("approve", [Address(spender), amount])

    def encode_balance_of(self, holder: Address) -> bytes:
        return self._token_abi.encode("balanceOf", [Address(holder)])

    @staticmethod
    def decode_uint(data: bytes) -> int:
        (value,) = abi_decode(["uint256"], data)
        return int(value)
