"""Drive a real negotiation on a real chain, with web3 and nothing else.

Used by the reconstruction tests to produce something worth reconstructing. This is *not* the
production path — the backend's relay, outbox and turn executor arrive in stage 2 — and it is
deliberately minimal: it signs with `eth_account` and submits with `web3`, so a reconstruction
failure points at the tool or the contract rather than at a shared helper both sides trust.

Note which key signs what. Each participant signs its own typed message; the relay signs the
*transaction* and never a message. That separation is the property the reconstruction tool checks
(`the_relay_signed_nothing`), so the driver has to actually honour it rather than approximate it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, cast

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.contract.contract import Contract, ContractFunction

from negotiation_protocol import (
    Accept,
    Close,
    Domain,
    Offer,
    SessionConfig,
    load_abi,
    to_hex32,
)


@dataclass(frozen=True)
class DrivenSession:
    """What the driver did, for a test to assert the reconstruction agrees with."""

    session_id: str
    config_hash: str
    buyer: str
    seller: str
    base_amount: int
    expires_at: int
    offer_digests: tuple[str, ...]
    settled_quote_amount: int | None
    close_reason: int | None


class NegotiationDriver:
    def __init__(self, w3: Web3, manifest: dict[str, Any], keys: tuple[str, ...]) -> None:
        self._w3 = w3
        self._manifest = manifest
        self.operator: LocalAccount = Account.from_key(keys[0])
        self.relay: LocalAccount = Account.from_key(keys[1])
        self.buyer: LocalAccount = Account.from_key(keys[2])
        self.seller: LocalAccount = Account.from_key(keys[3])

        self.exchange = self._contract("exchange_address", "NegotiationExchange")
        self.base_token = self._contract("base_token_address", "MockERC20")
        self.quote_token = self._contract("quote_token_address", "MockERC20")
        self._domain = Domain(int(manifest["chain_id"]), manifest["exchange_address"])

    def _contract(self, manifest_key: str, abi_name: str) -> Contract:
        return self._w3.eth.contract(
            address=Web3.to_checksum_address(self._manifest[manifest_key]),
            abi=load_abi(abi_name),
        )

    def _send(self, account: LocalAccount, call: ContractFunction) -> dict[str, Any]:
        transaction = call.build_transaction(
            {
                "from": account.address,
                "nonce": self._w3.eth.get_transaction_count(account.address),
            }
        )
        # web3 returns a `TxParams`; eth_account declares a narrower mapping for the same keys.
        # The two libraries disagree in their annotations, not in the data.
        signed = account.sign_transaction(cast("Any", transaction))
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
        assert receipt["status"] == 1, f"transaction reverted: {tx_hash.hex()}"
        return dict(receipt)

    # -----------------------------------------------------------------------------------
    # Setup and lifecycle
    # -----------------------------------------------------------------------------------

    def fund(self, base_amount: int, quote_amount: int) -> None:
        """Mint and approve. Fresh allowances per run, as the setup flow does."""
        self._send(self.operator, self.base_token.functions.mint(self.seller.address, base_amount))
        self._send(self.operator, self.quote_token.functions.mint(self.buyer.address, quote_amount))
        self._send(
            self.buyer, self.quote_token.functions.approve(self.exchange.address, quote_amount)
        )
        self._send(
            self.seller, self.base_token.functions.approve(self.exchange.address, base_amount)
        )

    def open_session(
        self, base_amount: int, duration: int = 1800, max_offers: int = 8
    ) -> SessionConfig:
        session_id = secrets.token_bytes(32)
        expires_at = int(self._w3.eth.get_block("latest")["timestamp"]) + duration
        config = SessionConfig(
            session_id=session_id,
            buyer=self.buyer.address,
            seller=self.seller.address,
            base_amount=base_amount,
            expires_at=expires_at,
            max_offers=max_offers,
        )
        self._send(
            self.operator,
            self.exchange.functions.createSession(
                (
                    session_id,
                    config.buyer,
                    config.seller,
                    config.base_amount,
                    config.expires_at,
                    config.max_offers,
                )
            ),
        )
        return config

    # -----------------------------------------------------------------------------------
    # Signed actions
    # -----------------------------------------------------------------------------------

    def record_offer(
        self,
        config: SessionConfig,
        sequence: int,
        proposer: LocalAccount,
        quote_amount: int,
        lifetime: int = 600,
    ) -> bytes:
        config_hash = self._config_hash(config)
        valid_until = min(
            int(self._w3.eth.get_block("latest")["timestamp"]) + lifetime, config.expires_at
        )
        offer = Offer(
            session_id=config.session_id,
            config_hash=config_hash,
            sequence=sequence,
            proposer=proposer.address,
            quote_amount=quote_amount,
            valid_until=valid_until,
        )
        digest = offer.digest(self._domain)
        signature = self._sign(proposer, digest)

        self._send(
            self.relay,
            self.exchange.functions.recordOffer(
                (
                    config.session_id,
                    config_hash,
                    sequence,
                    proposer.address,
                    quote_amount,
                    valid_until,
                ),
                signature,
            ),
        )
        return digest

    def accept(
        self, config: SessionConfig, sequence: int, actor: LocalAccount, offer_digest: bytes
    ) -> None:
        config_hash = self._config_hash(config)
        acceptance = Accept(
            session_id=config.session_id,
            config_hash=config_hash,
            sequence=sequence,
            actor=actor.address,
            offer_hash=offer_digest,
        )
        signature = self._sign(actor, acceptance.digest(self._domain))

        self._send(
            self.relay,
            self.exchange.functions.acceptAndSettle(
                (config.session_id, config_hash, sequence, actor.address, offer_digest), signature
            ),
        )

    def close(self, config: SessionConfig, sequence: int, actor: LocalAccount, reason: int) -> None:
        config_hash = self._config_hash(config)
        closure = Close(
            session_id=config.session_id,
            config_hash=config_hash,
            sequence=sequence,
            actor=actor.address,
            reason=reason,
        )
        signature = self._sign(actor, closure.digest(self._domain))

        self._send(
            self.relay,
            self.exchange.functions.closeSession(
                (config.session_id, config_hash, sequence, actor.address, reason), signature
            ),
        )

    def abort(self, config: SessionConfig, reason: int) -> None:
        """A lifecycle action: the operator calls it directly and it consumes no sequence."""
        self._send(self.operator, self.exchange.functions.abortSession(config.session_id, reason))

    # -----------------------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------------------

    def _config_hash(self, config: SessionConfig) -> bytes:
        return config.config_hash(
            self._manifest["base_token_address"], self._manifest["quote_token_address"]
        )

    def _sign(self, account: LocalAccount, digest: bytes) -> bytes:
        signed = Account._sign_hash(digest, account.key)
        return bytes(signed.signature)

    # -----------------------------------------------------------------------------------
    # Whole runs
    # -----------------------------------------------------------------------------------

    def run_to_settlement(
        self, base_amount: int = 10_000_000, opening: int = 88_000_000, agreed: int = 92_000_000
    ) -> DrivenSession:
        """Buyer opens low, seller counters, buyer accepts the counter.

        Two offers rather than one on purpose: the first digest is displaced by the second, so the
        reconstruction has to pick the *accepted* offer out of two recorded ones rather than the
        only one available.
        """
        self.fund(base_amount, agreed)
        config = self.open_session(base_amount)

        first = self.record_offer(config, 1, self.buyer, opening)
        counter = self.record_offer(config, 2, self.seller, agreed)
        self.accept(config, 3, self.buyer, counter)

        return DrivenSession(
            session_id=to_hex32(config.session_id),
            config_hash=to_hex32(self._config_hash(config)),
            buyer=config.buyer,
            seller=config.seller,
            base_amount=base_amount,
            expires_at=config.expires_at,
            offer_digests=(to_hex32(first), to_hex32(counter)),
            settled_quote_amount=agreed,
            close_reason=None,
        )

    def run_to_walk_away(
        self, base_amount: int = 10_000_000, opening: int = 88_000_000, reason: int = 1
    ) -> DrivenSession:
        """Buyer offers, seller walks away. The no-deal that A03 requires to be a real outcome."""
        self.fund(base_amount, 250_000_000)
        config = self.open_session(base_amount)

        first = self.record_offer(config, 1, self.buyer, opening)
        self.close(config, 2, self.seller, reason)

        return DrivenSession(
            session_id=to_hex32(config.session_id),
            config_hash=to_hex32(self._config_hash(config)),
            buyer=config.buyer,
            seller=config.seller,
            base_amount=base_amount,
            expires_at=config.expires_at,
            offer_digests=(to_hex32(first),),
            settled_quote_amount=None,
            close_reason=reason,
        )
