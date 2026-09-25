#!/usr/bin/env python3
"""Generate `fixtures/eip712.v1.json`, the cross-language digest fixture.

    uv run python packages/protocol/tools/generate_eip712_fixtures.py

The file this writes is the arbiter between four independent EIP-712 implementations
(docs/test_strategy.md section 5):

    negotiation_protocol.eip712   keccak and ABI encoding from docs/protocol.md sections 3 and 4
    pytest                        `eth_account.messages.encode_typed_data`
    Vitest                        `viem`'s `hashTypedData`
    Forge                         OpenZeppelin's `EIP712._hashTypedDataV4`, via `hashOffer`

Four implementations agreeing on one digest is evidence that the type strings, the field order and
the `configHash` encoding are what the protocol document says. One of them disagreeing is a build
failure in whichever language drifted, which is the whole point: a field renamed in Python alone
would otherwise surface as an unexplained `BadSignature` during a live run.

**Regenerating this file is a protocol event, not a chore.** Every digest and signature in it is
determined by the domain, the type strings and the `configHash` encoding, so if the output changes,
one of those changed — which is a protocol version bump and a new deployment (docs/protocol.md
section 15). CI regenerates the file and fails on any diff.

The two private keys are `0xB0B` and `0x5E11E4`, the same worthless constants the Foundry suite uses
in `contracts/test/BaseTest.sol`. They are written out as full 32-byte hex so every language can
load them without padding rules of its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from negotiation_protocol.eip712 import (
    ACCEPT_TYPE,
    CLOSE_TYPE,
    OFFER_TYPE,
    Accept,
    Close,
    Domain,
    Offer,
    SessionConfig,
    to_hex32,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "eip712.v1.json"

CHAIN_ID = 31337

# The addresses a fresh Anvil gives the first three deployments of `script/Deploy.s.sol`, in its
# order: base token, quote token, exchange. Recognisable as local fixtures, and they let the Forge
# test place the contracts at exactly these addresses (`vm.deployCodeTo`) so the domain separator it
# computes is the fixture's own.
BASE_TOKEN = to_checksum_address("0x5fbdb2315678afecb367f032d93f642f64180aa3")
QUOTE_TOKEN = to_checksum_address("0xe7f1725e7734ce288f8367e1bb143e90bb3f0512")
VERIFYING_CONTRACT = to_checksum_address("0x9fe46736679d2d9a65f0992f2272de9f3c7fa6e0")
OPERATOR = to_checksum_address("0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266")

# Manufactured values, from the defaults in the system spec section 2.3: 10 mASSET against a
# negotiated mUSD amount, an eight-offer session lasting 1,800 s.
SESSION_ID = keccak(text="negotiation-protocol-fixture-session-1")
CHAIN_TIME = 1_760_000_000
SESSION_DURATION = 1_800
OFFER_LIFETIME = 600
BASE_AMOUNT = 10_000_000
MAX_OFFERS = 8
QUOTE_AMOUNT = 92_000_000
CLOSE_REASON = 1  # terms_unacceptable (docs/protocol.md section 10)

BUYER_KEY = 0xB0B
SELLER_KEY = 0x5E11E4


def _key_hex(key: int) -> str:
    return "0x" + key.to_bytes(32, "big").hex()


def _signed(private_key: int, message_digest: bytes) -> str:
    """Sign a precomputed digest. RFC 6979 deterministic `k`, so the output is reproducible."""
    signature = Account._sign_hash(message_digest, _key_hex(private_key))
    return str(signature.signature.to_0x_hex())


def build() -> dict[str, Any]:
    buyer = Account.from_key(_key_hex(BUYER_KEY)).address
    seller = Account.from_key(_key_hex(SELLER_KEY)).address
    expires_at = CHAIN_TIME + SESSION_DURATION
    valid_until = CHAIN_TIME + OFFER_LIFETIME

    domain = Domain(chain_id=CHAIN_ID, verifying_contract=VERIFYING_CONTRACT)
    config = SessionConfig(
        session_id=SESSION_ID,
        buyer=buyer,
        seller=seller,
        base_amount=BASE_AMOUNT,
        expires_at=expires_at,
        max_offers=MAX_OFFERS,
    )
    config_hash = config.config_hash(BASE_TOKEN, QUOTE_TOKEN)

    offer = Offer(
        session_id=SESSION_ID,
        config_hash=config_hash,
        sequence=1,
        proposer=buyer,
        quote_amount=QUOTE_AMOUNT,
        valid_until=valid_until,
    )
    offer_digest = offer.digest(domain)

    # `offerHash` in an Accept is the full Offer digest, domain included, so both signatures
    # authorise the same trade on the same chain and contract (docs/protocol.md section 4).
    #
    # The Accept and the Close both carry sequence 2 on purpose: they are the two possible replies
    # to the offer at sequence 1, and the Forge test replays each on its own session.
    acceptance = Accept(
        session_id=SESSION_ID,
        config_hash=config_hash,
        sequence=2,
        actor=seller,
        offer_hash=offer_digest,
    )
    accept_digest = acceptance.digest(domain)

    closure = Close(
        session_id=SESSION_ID,
        config_hash=config_hash,
        sequence=2,
        actor=seller,
        reason=CLOSE_REASON,
    )
    close_digest = closure.digest(domain)

    return {
        "fixture_version": "1",
        "protocol_version": "1",
        "generated_by": "packages/protocol/tools/generate_eip712_fixtures.py",
        "note": (
            "Manufactured test values. The two private keys are worthless constants shared with "
            "contracts/test/BaseTest.sol. Every digest here is determined by the domain, the "
            "type strings and the configHash encoding: a change to this file is a protocol "
            "version bump (docs/protocol.md section 15)."
        ),
        "domain": {
            "name": domain.name,
            "version": domain.version,
            "chain_id": domain.chain_id,
            "verifying_contract": domain.verifying_contract,
        },
        "domain_separator": to_hex32(domain.separator()),
        "type_strings": {"offer": OFFER_TYPE, "accept": ACCEPT_TYPE, "close": CLOSE_TYPE},
        "type_hashes": {
            "offer": to_hex32(keccak(text=OFFER_TYPE)),
            "accept": to_hex32(keccak(text=ACCEPT_TYPE)),
            "close": to_hex32(keccak(text=CLOSE_TYPE)),
        },
        "deployment": {
            "operator": OPERATOR,
            "base_token": BASE_TOKEN,
            "quote_token": QUOTE_TOKEN,
            "token_decimals": 6,
        },
        "participants": {
            "buyer": {"address": buyer, "private_key": _key_hex(BUYER_KEY)},
            "seller": {"address": seller, "private_key": _key_hex(SELLER_KEY)},
        },
        "chain_time": CHAIN_TIME,
        "session_config": {
            "session_id": to_hex32(config.session_id),
            "buyer": config.buyer,
            "seller": config.seller,
            "base_amount": str(config.base_amount),
            "expires_at": config.expires_at,
            "max_offers": config.max_offers,
        },
        "config_hash": to_hex32(config_hash),
        "messages": {
            "offer": {
                "message": {
                    "session_id": to_hex32(offer.session_id),
                    "config_hash": to_hex32(offer.config_hash),
                    "sequence": offer.sequence,
                    "proposer": offer.proposer,
                    "quote_amount": str(offer.quote_amount),
                    "valid_until": offer.valid_until,
                },
                "digest": to_hex32(offer_digest),
                "signer": "buyer",
                "signature": _signed(BUYER_KEY, offer_digest),
            },
            "accept": {
                "message": {
                    "session_id": to_hex32(acceptance.session_id),
                    "config_hash": to_hex32(acceptance.config_hash),
                    "sequence": acceptance.sequence,
                    "actor": acceptance.actor,
                    "offer_hash": to_hex32(acceptance.offer_hash),
                },
                "digest": to_hex32(accept_digest),
                "signer": "seller",
                "signature": _signed(SELLER_KEY, accept_digest),
            },
            "close": {
                "message": {
                    "session_id": to_hex32(closure.session_id),
                    "config_hash": to_hex32(closure.config_hash),
                    "sequence": closure.sequence,
                    "actor": closure.actor,
                    "reason": closure.reason,
                },
                "digest": to_hex32(close_digest),
                "signer": "seller",
                "signature": _signed(SELLER_KEY, close_digest),
            },
        },
    }


def main() -> int:
    previous = FIXTURE_PATH.read_text(encoding="utf-8") if FIXTURE_PATH.is_file() else None
    content = json.dumps(build(), indent=2) + "\n"
    FIXTURE_PATH.write_text(content, encoding="utf-8")

    if previous is not None and previous != content:
        print(
            f"CHANGED {FIXTURE_PATH}\n"
            "  Every value in this file is determined by the domain, the type strings and the "
            "configHash encoding.\n  If it changed, one of those changed: that is a protocol "
            "version bump and a new deployment (docs/protocol.md section 15)."
        )
    else:
        print(f"wrote {FIXTURE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
