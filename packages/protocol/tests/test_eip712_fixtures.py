"""The Python third of the three-language digest check (docs/test_strategy.md section 5).

`fixtures/eip712.v1.json` is written by `tools/generate_eip712_fixtures.py`, which computes every
digest by hand from docs/protocol.md sections 3 and 4. This module checks the same file with
`eth_account`'s own EIP-712 implementation, Vitest checks it with viem's, and Forge checks it with
OpenZeppelin's. Four implementations agreeing is evidence that the type strings, the field order
and the `configHash` encoding are what the document says.

The field-name mapping gets its own assertions rather than being assumed. Solidity uses
`quoteAmount`, the fixture and Python use `quote_amount`, TypeScript will use `quoteAmount` again;
the conversion is mechanical, and the point of deriving the typed-data structure *from the
fixture's own type strings* is that a field renamed on one side leaves a name with no counterpart
rather than a digest that quietly differs.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from negotiation_protocol import load_fixture

FIXTURE = load_fixture("eip712.v1.json")

#: Solidity value types that appear in the three type strings, and how a JSON value becomes one.
_INTEGER_TYPES = ("uint8", "uint16", "uint64", "uint256")


def _snake(camel: str) -> str:
    """`quoteAmount` -> `quote_amount`. The only naming convention this project converts."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def _parse_type_string(type_string: str) -> tuple[str, list[dict[str, str]]]:
    """`Offer(bytes32 sessionId,uint64 sequence)` -> `("Offer", [{type, name}, …])`.

    Parsed rather than restated so that the type string in the fixture — the exact bytes that are
    keccak-hashed into the type hash — is the same text this test derives its field list from.
    A hand-written field list could agree with the digest while disagreeing with the string.
    """
    match = re.fullmatch(r"(\w+)\((.*)\)", type_string)
    assert match is not None, f"malformed type string: {type_string!r}"
    primary, body = match.group(1), match.group(2)
    fields = []
    for part in body.split(","):
        sol_type, name = part.split(" ")
        fields.append({"type": sol_type, "name": name})
    return primary, fields


def _typed_data(kind: str) -> dict[str, Any]:
    """Build an EIP-712 `full_message` for one fixture message."""
    primary, fields = _parse_type_string(FIXTURE["type_strings"][kind])
    stored: dict[str, Any] = FIXTURE["messages"][kind]["message"]

    expected_keys = {_snake(field["name"]) for field in fields}
    assert set(stored) == expected_keys, (
        f"{kind}: fixture keys {sorted(stored)} do not match the type string's fields "
        f"{sorted(expected_keys)} after snake_case conversion"
    )

    message = {
        field["name"]: (
            int(stored[_snake(field["name"])])
            if field["type"] in _INTEGER_TYPES
            else stored[_snake(field["name"])]
        )
        for field in fields
    }

    domain = FIXTURE["domain"]
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            primary: fields,
        },
        "primaryType": primary,
        "domain": {
            "name": domain["name"],
            "version": domain["version"],
            "chainId": domain["chain_id"],
            "verifyingContract": domain["verifying_contract"],
        },
        "message": message,
    }


class TestDomain:
    def test_the_domain_is_the_one_in_the_protocol_document(self) -> None:
        # docs/protocol.md section 4. A change to any of these four values changes every digest
        # the system has ever produced, so they are asserted literally rather than read back.
        domain = FIXTURE["domain"]
        assert domain["name"] == "AgentNegotiationSandbox"
        assert domain["version"] == "1"
        assert domain["chain_id"] == 31337
        assert domain["verifying_contract"] == to_checksum_address(domain["verifying_contract"])

    def test_the_type_hashes_are_keccak_of_the_type_strings(self) -> None:
        for kind, type_string in FIXTURE["type_strings"].items():
            assert FIXTURE["type_hashes"][kind] == "0x" + keccak(text=type_string).hex(), kind

    def test_the_type_strings_are_byte_exact(self) -> None:
        # Byte-exact, including the absence of spaces after commas: the string is hashed, so a
        # cosmetic difference is a different protocol (docs/protocol.md section 4).
        assert FIXTURE["type_strings"]["offer"] == (
            "Offer(bytes32 sessionId,bytes32 configHash,uint64 sequence,"
            "address proposer,uint256 quoteAmount,uint64 validUntil)"
        )
        assert FIXTURE["type_strings"]["accept"] == (
            "Accept(bytes32 sessionId,bytes32 configHash,uint64 sequence,"
            "address actor,bytes32 offerHash)"
        )
        assert FIXTURE["type_strings"]["close"] == (
            "Close(bytes32 sessionId,bytes32 configHash,uint64 sequence,address actor,uint8 reason)"
        )


class TestConfigHash:
    def test_the_config_hash_binds_the_session_to_this_exchange(self) -> None:
        """docs/protocol.md section 3, recomputed with `eth_abi` from the fixture's own fields.

        The two token addresses are part of the hash, so a session signed for one deployment
        cannot be replayed against another that happens to share a `sessionId` (A07).
        """
        from eth_abi.abi import encode as abi_encode

        config = FIXTURE["session_config"]
        deployment = FIXTURE["deployment"]
        recomputed = keccak(
            abi_encode(
                [
                    "bytes32",
                    "address",
                    "address",
                    "address",
                    "address",
                    "uint256",
                    "uint64",
                    "uint16",
                ],
                [
                    bytes.fromhex(config["session_id"][2:]),
                    config["buyer"],
                    config["seller"],
                    deployment["base_token"],
                    deployment["quote_token"],
                    int(config["base_amount"]),
                    config["expires_at"],
                    config["max_offers"],
                ],
            )
        )
        assert FIXTURE["config_hash"] == "0x" + recomputed.hex()

    def test_every_message_carries_the_session_config_hash(self) -> None:
        # Step 3 of the verification order: a message whose `configHash` differs from the stored
        # one is refused, so a fixture message that carried a different hash would be untestable
        # against the contract (docs/protocol.md section 8.1).
        for kind, entry in FIXTURE["messages"].items():
            assert entry["message"]["config_hash"] == FIXTURE["config_hash"], kind
            assert entry["message"]["session_id"] == FIXTURE["session_config"]["session_id"], kind


@pytest.mark.parametrize("kind", ["offer", "accept", "close"])
class TestMessages:
    def test_the_digest_matches_eth_accounts_implementation(self, kind: str) -> None:
        signable = encode_typed_data(full_message=_typed_data(kind))
        signer = FIXTURE["participants"][FIXTURE["messages"][kind]["signer"]]
        signed = Account.sign_message(signable, signer["private_key"])

        assert signed.message_hash.to_0x_hex() == FIXTURE["messages"][kind]["digest"]

    def test_the_signature_is_reproducible(self, kind: str) -> None:
        # ECDSA here is deterministic (RFC 6979), so the same key over the same digest gives the
        # same 65 bytes. That is what lets the fixture pin a signature rather than only a digest,
        # and what lets CI regenerate the file and diff it.
        signable = encode_typed_data(full_message=_typed_data(kind))
        signer = FIXTURE["participants"][FIXTURE["messages"][kind]["signer"]]
        signed = Account.sign_message(signable, signer["private_key"])

        assert signed.signature.to_0x_hex() == FIXTURE["messages"][kind]["signature"]

    def test_the_signature_recovers_to_the_party_the_message_names(self, kind: str) -> None:
        """Step 5 of the verification order, off-chain.

        The contract recovers the signer and requires it to equal the address inside the message.
        A fixture whose signature recovered to anyone else would be a fixture of a rejected
        action (docs/protocol.md section 8.1).
        """
        entry = FIXTURE["messages"][kind]
        signable = encode_typed_data(full_message=_typed_data(kind))
        recovered = Account.recover_message(
            signable, signature=bytes.fromhex(entry["signature"][2:])
        )

        named_field = "proposer" if kind == "offer" else "actor"
        assert recovered == entry["message"][named_field]
        assert recovered == FIXTURE["participants"][entry["signer"]]["address"]

    def test_a_flipped_bit_in_the_digest_recovers_to_someone_else(self, kind: str) -> None:
        """The negative half: the fixture pins a digest, and the digest is what authorises.

        Without this, a bug that made every message hash to the same value would pass every
        assertion above.
        """
        entry = FIXTURE["messages"][kind]
        mutated = _typed_data(kind)
        mutated["message"]["sequence"] = int(mutated["message"]["sequence"]) + 1

        signable = encode_typed_data(full_message=mutated)
        signer = FIXTURE["participants"][entry["signer"]]
        signed = Account.sign_message(signable, signer["private_key"])

        assert signed.message_hash.to_0x_hex() != entry["digest"]
        assert signed.signature.to_0x_hex() != entry["signature"]


class TestAcceptLinkage:
    def test_the_acceptance_references_the_offers_full_digest(self) -> None:
        """docs/protocol.md section 4: `offerHash` is the whole Offer digest, domain included.

        This is what makes both signatures authorise the same trade on the same chain and the
        same contract. A linkage over the struct hash alone would let an acceptance signed for
        one deployment execute against another.
        """
        assert (
            FIXTURE["messages"]["accept"]["message"]["offer_hash"]
            == FIXTURE["messages"]["offer"]["digest"]
        )

    def test_the_acceptance_is_signed_by_the_counterparty(self) -> None:
        # Self-acceptance is invalid (docs/protocol.md rule 5.6), so the fixture's offer and
        # acceptance must come from opposite sides for the pair to be executable at all.
        assert FIXTURE["messages"]["offer"]["signer"] == "buyer"
        assert FIXTURE["messages"]["accept"]["signer"] == "seller"
        assert (
            FIXTURE["messages"]["offer"]["message"]["proposer"]
            != FIXTURE["messages"]["accept"]["message"]["actor"]
        )


class TestSequenceAndTime:
    def test_the_offer_opens_the_negotiation_and_the_acceptance_follows_it(self) -> None:
        # Rules 5.1 and 5.2: the buyer has the first turn at sequence 1, and every subsequent
        # signed action carries exactly one more.
        assert FIXTURE["messages"]["offer"]["message"]["sequence"] == 1
        assert FIXTURE["messages"]["accept"]["message"]["sequence"] == 2

    def test_the_offer_expires_within_the_session(self) -> None:
        # docs/protocol.md section 6: `validUntil <= expiresAt`, and both strictly after the
        # chain time at which the fixture is meant to be replayed.
        chain_time = FIXTURE["chain_time"]
        valid_until = FIXTURE["messages"]["offer"]["message"]["valid_until"]
        expires_at = FIXTURE["session_config"]["expires_at"]

        assert chain_time < valid_until <= expires_at
