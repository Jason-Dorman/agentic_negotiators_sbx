"""The request bodies of the agent internal API, built from the run's records.

Pure functions, so that provisioning at run creation and the re-provisioning after an agent restart
(ADR-048) send the same body — the agent compares a re-provisioning with the first one, less
`expected_address`, and refuses any other difference as `idempotency_conflict`.

Only `provision_body` carries a mandate, and only the one party's it is given: the backend sends a
mandate to an agent once, at provisioning (docs/architecture.md section 5.1), and the caller hands
this function that party's row alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from api.db.enums import Party
from api.db.records import (
    ChainEventRecord,
    DeploymentRecord,
    MandateVersionRecord,
    RunRecord,
    WalletRecord,
)
from negotiation_protocol import Address

#: `runs.limits` keys, as the agent's provisioning body names them.
LIMIT_KEYS: Final = (
    "model_call_ceiling",
    "model_spend_ceiling_usd",
    "model_timeout_s",
    "repair_attempts",
)


def _policy(run: RunRecord, party: Party) -> tuple[str, str | None, str | None]:
    if party == Party.BUYER:
        return run.buyer_policy.value, run.buyer_model_id, run.buyer_effort
    return run.seller_policy.value, run.seller_model_id, run.seller_effort


def provision_body(
    run: RunRecord,
    deployment: DeploymentRecord,
    party: Party,
    mandate: MandateVersionRecord,
    *,
    key_ref: str,
    initial_balances: Mapping[str, str],
    allowance_minor: str,
    expected_address: Address | None,
) -> dict[str, Any]:
    """`POST /internal/runs/{id}/provision` (api_contract section 6)."""
    if mandate.party != party or mandate.run_id != run.id:
        raise ValueError("a provisioning body carries its own party's mandate and no other")
    policy, model_id, effort = _policy(run, party)
    public = run.public_config
    return {
        "role": party.value,
        "policy": policy,
        "model_id": model_id,
        "effort": effort,
        "limits": {key: run.limits[key] for key in LIMIT_KEYS},
        "expected_session": {
            "chain_id": deployment.chain_id,
            "exchange_address": str(deployment.exchange_address),
            "base_token": str(deployment.base_token_address),
            "quote_token": str(deployment.quote_token_address),
            "base_amount_minor": str(public["base_amount_minor"]),
            "max_offers": int(public["max_offers"]),
            "session_duration_s": int(public["session_duration_s"]),
            "offer_lifetime_s": int(public["offer_lifetime_s"]),
        },
        "key_ref": key_ref,
        "expected_address": None if expected_address is None else str(expected_address),
        "mandate_version_id": str(mandate.id),
        "mandate": mandate.as_document(),
        "initial_balances": {
            "base_minor": str(initial_balances["base_minor"]),
            "quote_minor": str(initial_balances["quote_minor"]),
        },
        "allowance_minor": str(allowance_minor),
    }


def reprovision_body(
    run: RunRecord,
    deployment: DeploymentRecord,
    mandate: MandateVersionRecord,
    wallet: WalletRecord,
) -> dict[str, Any]:
    """The same body again, from what was stored, with the stored address to reproduce."""
    return provision_body(
        run,
        deployment,
        wallet.party,
        mandate,
        key_ref=wallet.key_ref,
        initial_balances={
            "base_minor": str(int(wallet.initial_base_minor)),
            "quote_minor": str(int(wallet.initial_quote_minor)),
        },
        allowance_minor=str(int(wallet.allowance_minor)),
        expected_address=wallet.address,
    )


def approve_session_body(opened: ChainEventRecord, opened_at_ts: int) -> dict[str, Any]:
    """`approve-session`, from the canonical `SessionOpened` row and its block's timestamp
    (ADR-044, ADR-048)."""
    if opened.event_name != "SessionOpened":
        raise ValueError("a session is approved from its SessionOpened event")
    args = opened.decoded
    return {
        "session_id": str(args["sessionId"]),
        "buyer": str(args["buyer"]),
        "seller": str(args["seller"]),
        "base_token": str(args["baseToken"]),
        "quote_token": str(args["quoteToken"]),
        "base_amount_minor": str(args["baseAmount"]),
        "expires_at_ts": int(args["expiresAt"]),
        "max_offers": int(args["maxOffers"]),
        "config_hash": str(args["configHash"]),
        "opened_at_ts": opened_at_ts,
    }


def setup_approval_body(
    nonce: int, gas_limit: int, max_fee_per_gas_wei: int, max_priority_fee_per_gas_wei: int
) -> dict[str, Any]:
    """`setup-approval`: only what the backend must know about the chain (ADR-040)."""
    return {
        "nonce": nonce,
        "gas_limit": gas_limit,
        "max_fee_per_gas_wei": str(max_fee_per_gas_wei),
        "max_priority_fee_per_gas_wei": str(max_priority_fee_per_gas_wei),
    }
