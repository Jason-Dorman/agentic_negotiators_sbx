"""The relay's fee arithmetic (ADR-050, ADR-054), its signer, and the chain settings.

Every boundary is pinned with worked numbers rather than recomputed with the formula under test.
"""

from __future__ import annotations

import copy
import pickle

import pytest
from eth_account import Account

from api.chain import FeeQuote
from api.config import GWEI, ChainSettings, RelayPolicy, SettingsError, load_chain_settings
from api.relay import Fees, LocalTransactionSigner, bumped_fees, gas_limit, initial_fees
from negotiation_protocol.key_refs import KeyReferenceError

KEY = "0x" + "59" * 32


def test_the_decided_defaults() -> None:
    policy = RelayPolicy()
    assert policy.replace_after_blocks == 3  # ADR-050
    assert policy.max_fee_per_gas_wei == 100 * GWEI  # ADR-050
    assert policy.fallback_gas_limit == 500_000  # ADR-054


def test_initial_fees_are_twice_the_base_fee_plus_the_tip() -> None:
    fees = initial_fees(
        FeeQuote(base_fee_per_gas=10 * GWEI, max_priority_fee_per_gas=GWEI), RelayPolicy()
    )
    assert fees == Fees(max_fee_per_gas=21 * GWEI, max_priority_fee_per_gas=GWEI)


def test_initial_fees_never_exceed_the_ceiling() -> None:
    policy = RelayPolicy(max_fee_per_gas_wei=15 * GWEI)
    fees = initial_fees(
        FeeQuote(base_fee_per_gas=10 * GWEI, max_priority_fee_per_gas=20 * GWEI), policy
    )
    assert fees == Fees(max_fee_per_gas=15 * GWEI, max_priority_fee_per_gas=15 * GWEI)


def test_a_bump_is_an_eighth_rounded_up() -> None:
    assert bumped_fees(Fees(800, 80), RelayPolicy()) == Fees(900, 90)
    assert bumped_fees(Fees(801, 81), RelayPolicy()) == Fees(902, 92)  # 901.125 and 91.125, up


def test_a_bump_clipped_by_the_ceiling_is_used_while_it_clears_the_nodes_ten_percent() -> None:
    assert bumped_fees(Fees(1000, 100), RelayPolicy(max_fee_per_gas_wei=1100)) == Fees(1100, 113)
    assert bumped_fees(Fees(1000, 100), RelayPolicy(max_fee_per_gas_wei=1099)) is None
    assert bumped_fees(Fees(1000, 100), RelayPolicy(max_fee_per_gas_wei=1000)) is None


def test_the_tip_never_exceeds_the_fee_cap_after_a_bump() -> None:
    assert bumped_fees(Fees(1000, 1000), RelayPolicy(max_fee_per_gas_wei=1100)) == Fees(1100, 1100)


def test_gas_limit_is_the_estimate_plus_a_quarter_or_the_fallback() -> None:
    assert gas_limit(100_000, RelayPolicy()) == 125_000
    assert gas_limit(100_001, RelayPolicy()) == 125_002  # rounded up
    assert gas_limit(None, RelayPolicy()) == 500_000
    assert gas_limit(None, RelayPolicy(fallback_gas_limit=300_000)) == 300_000


def test_the_signer_signs_and_gives_nothing_away() -> None:
    signer = LocalTransactionSigner.from_reference("env:RELAY_KEY", {"RELAY_KEY": KEY}, "relay")
    assert signer.address == Account.from_key(KEY).address
    assert KEY[2:] not in repr(signer)
    assert repr(signer).startswith("TransactionSigner(relay, address=")
    for attempt in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError, match="cannot be serialised or copied"):
            attempt(signer)
    signed = signer.sign_transaction(
        {
            "type": 2,
            "chainId": 31337,
            "nonce": 0,
            "to": signer.address,
            "value": 0,
            "data": b"",
            "gas": 21_000,
            "maxFeePerGas": 2,
            "maxPriorityFeePerGas": 1,
        }
    )
    assert Account.recover_transaction(signed.raw_transaction) == signer.address


def test_a_key_pasted_as_its_reference_is_refused_without_repeating_it() -> None:
    with pytest.raises(KeyReferenceError) as refused:
        LocalTransactionSigner.from_reference(f"env:RELAY_KEY={KEY}", {}, "relay")
    assert KEY[2:] not in str(refused.value)


def test_settings_refuse_a_key_where_a_reference_belongs_and_do_not_quote_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAIN_RPC_URL", "http://127.0.0.1:8545")
    monkeypatch.setenv("RELAY_KEY_REF", KEY)
    monkeypatch.setenv("OPERATOR_KEY_REF", "env:OPERATOR_PRIVATE_KEY")
    with pytest.raises(SettingsError) as refused:
        load_chain_settings()
    assert "RELAY_KEY_REF" in str(refused.value)
    assert KEY[2:] not in str(refused.value)


def test_settings_carry_the_policies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAIN_RPC_URL", "http://127.0.0.1:8545")
    monkeypatch.setenv("RELAY_KEY_REF", "env:RELAY_PRIVATE_KEY")
    monkeypatch.setenv("OPERATOR_KEY_REF", "keystore:/run/secrets/operator.json")
    monkeypatch.setenv("CONFIRMATION_THRESHOLD", "2")
    monkeypatch.setenv("RELAY_REPLACE_AFTER_BLOCKS", "5")
    settings = load_chain_settings()
    assert isinstance(settings, ChainSettings)
    assert settings.relay_policy() == RelayPolicy(replace_after_blocks=5)
    assert settings.indexer_policy().default_confirmation_threshold == 2
    assert settings.indexer_policy().log_chunk_blocks == 2_000


@pytest.mark.parametrize("value", ["00" * 32, "ff" * 32])
def test_a_reference_to_bytes_that_are_no_key_is_a_key_reference_error(value: str) -> None:
    with pytest.raises(KeyReferenceError, match="does not hold a valid private key") as refused:
        LocalTransactionSigner.from_reference("env:RELAY_KEY", {"RELAY_KEY": value}, "relay")
    assert value not in str(refused.value)
