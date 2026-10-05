"""The operator API's response shapes (api_contract section 2), as the OpenAPI document states them.

Every response a route returns is validated against its model before it leaves, and a model forbids
fields it does not declare, so a field added to a view without being added here fails the response
rather than reaching a client — the route layer's half of the rule that nothing private leaves the
server by accident. Amounts are decimal strings, chain timestamps integers, application timestamps
ISO 8601 (api_contract section 1).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Party = Literal["buyer", "seller"]
Actor = Literal["buyer", "seller", "operator", "anyone"]
RunStateName = Literal[
    "draft",
    "validated",
    "preparing",
    "running",
    "paused",
    "recovery_required",
    "terminal",
    "failed_setup",
]
OutcomeKindName = Literal["pending", "settled", "closed", "expired", "aborted"]


# ---------------------------------------------------------------------------------------------
# Errors and operations (section 1)
# ---------------------------------------------------------------------------------------------


class ErrorBody(_Model):
    code: str
    message: str
    details: dict[str, Any]
    request_id: str


class ErrorEnvelope(_Model):
    error: ErrorBody


class Operation(_Model):
    operation_id: str
    kind: str
    run_id: str | None
    status: Literal["pending", "running", "succeeded", "failed"]
    created_at: str
    updated_at: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None


# ---------------------------------------------------------------------------------------------
# System (section 2.1)
# ---------------------------------------------------------------------------------------------


class Health(_Model):
    status: Literal["ok", "degraded"]
    version: str
    chain_id: int
    rpc_ok: bool
    db_ok: bool
    agent_a_ok: bool
    agent_b_ok: bool


class Deployment(_Model):
    deployment_id: str
    chain_id: int
    protocol_version: str
    exchange_address: str
    base_token_address: str
    quote_token_address: str
    operator_address: str
    relay_address: str
    code_hashes: dict[str, str]
    compiler: dict[str, Any]
    explorer_base_url: str | None
    ens: dict[str, Any] | None
    deployed_at: str


class DeploymentList(_Model):
    deployments: list[Deployment]


class ScenarioSummary(_Model):
    scenario_id: str
    name: str
    description: str
    #: Always null: feasibility is the evaluator's alone and never shown on a setup route.
    feasibility_hint: None


class ScenarioList(_Model):
    scenarios: list[ScenarioSummary]


class Scenario(_Model):
    """**Private** (both mandates): behind the observer reveal header."""

    scenario_id: str
    name: str
    description: str
    public_config: dict[str, Any]
    buyer: dict[str, Any]
    seller: dict[str, Any]


# ---------------------------------------------------------------------------------------------
# The run resource (section 2.2)
# ---------------------------------------------------------------------------------------------


class Outcome(_Model):
    kind: OutcomeKindName
    reason_code: int | None
    reason: str | None
    actor: Actor | None


class DeploymentRef(_Model):
    deployment_id: str
    chain_id: int
    exchange_address: str
    explorer_base_url: str | None


class ActiveOffer(_Model):
    offer_hash: str
    proposer: Party
    quote_amount_minor: str
    valid_until_ts: int
    sequence: int


class Session(_Model):
    session_id: str
    config_hash: str
    buyer_address: str
    seller_address: str
    base_amount_minor: str
    expires_at_ts: int
    max_offers: int
    status: Literal["open", "settled", "closed", "expired", "aborted"]
    sequence: int
    offer_count: int
    active_offer: ActiveOffer | None
    opened_tx_hash: str


class Balances(_Model):
    base_minor: str
    quote_minor: str


class PartyView(_Model):
    address: str
    policy: Literal["deterministic", "model"]
    model_id: str | None
    effort: str | None
    balances: Balances
    current_action: Literal["idle", "deciding", "signing", "awaiting_confirmation"]
    decision_status: str | None


class Parties(_Model):
    buyer: PartyView
    seller: PartyView


class TxView(_Model):
    tx_hash: str
    status: str
    block_number: int | None
    block_hash: str | None
    confirmations: int
    explorer_url: str | None


class TimelineEntry(_Model):
    sequence: int
    kind: Literal["offer", "accept", "close", "expire", "abort", "settle", "execution_failure"]
    actor: Actor
    quote_amount_minor: str | None
    valid_until_ts: int | None
    offer_hash: str | None
    references_offer_hash: str | None
    reason_code: int | None
    reason: str | None
    tx: TxView
    sentence: str | None
    recorded_at: str


class MetricStrip(_Model):
    """FR-U5's three cost groups: model, chain (setup included) and RPC (ADR-061)."""

    recorded_offers: int
    decision_time_ms: int
    chain_wait_ms: int
    model_calls: int
    model_cost_estimated_usd: str | None
    model_cost_reported_usd: str | None
    gas_used: int
    fee_wei: str
    rpc_requests: int
    rpc_cost_estimated_usd: str | None


class Labels(_Model):
    test_assets: Literal[True]
    simulated_economics: Literal[True]
    fixture: bool
    operator_abort_power: Literal[True]


class Run(_Model):
    run_id: str
    name: str
    parent_run_id: str | None
    created_at: str
    state: RunStateName
    state_cause: str | None
    mode: Literal["live", "fixture", "replay"]
    outcome: Outcome
    deployment: DeploymentRef
    session: Session | None
    parties: Parties
    timeline: list[TimelineEntry]
    metrics: MetricStrip
    labels: Labels


class RunSummary(_Model):
    run_id: str
    name: str
    parent_run_id: str | None
    batch_id: str | None
    scenario_id: str | None
    deployment_id: str
    created_at: str
    state: RunStateName
    state_cause: str | None
    mode: Literal["live", "fixture"]
    outcome: Outcome


class RunList(_Model):
    runs: list[RunSummary]
    next_cursor: str | None


class Check(_Model):
    check: str
    ok: bool
    detail: str | None = None


class ValidationReport(_Model):
    ok: bool
    checks: list[Check]


class AbortRequest(_Model):
    reason: str = Field(
        default="operator_request",
        description="One of the four abort reason names of protocol section 10.",
    )


# ---------------------------------------------------------------------------------------------
# Observer-only views (section 2.2)
# ---------------------------------------------------------------------------------------------


class MandateView(_Model):
    mandate_version_id: str
    version: int
    reservation_price_minor: str
    min_remaining_inventory_minor: str
    instructions: str


class Evaluator(_Model):
    feasible: bool | None
    feasible_interval_minor: list[str] | None
    feasible_surplus_minor: str | None


class Mandates(_Model):
    run_id: str
    buyer: MandateView
    seller: MandateView
    evaluator: Evaluator


class Validation(_Model):
    ok: bool
    code: str | None
    feedback: str | None


class DecisionRecord(_Model):
    decision_id: str
    turn: int | None
    party: Party
    attempt: int
    status: Literal["valid", "invalid", "model_failed"]
    policy: Literal["deterministic", "model"]
    model_id: str | None
    prompt_template_version: str | None
    effort: str | None
    request_hash: str | None
    raw_response: Any
    validation: Validation
    stop_reason: str | None
    usage: dict[str, Any] | None
    cost_estimated_usd: str | None
    cost_reported_usd: str | None
    latency_ms: int | None
    requested_at: str
    #: The observation the attempt was decided on; the agent's own hash is checked equal to it.
    observation_hash: str | None
    authorized: bool
    label: Literal["private_operational_record_not_an_authorized_offer"]


class Decisions(_Model):
    decisions: list[DecisionRecord]


class Metrics(_Model):
    """Spec 11.2 with ADR-061's RPC figures. The six figures that need both mandates are null
    without the observer reveal header (api_contract section 2.2); with it, null where the run has
    none yet."""

    run_id: str
    recorded_offers: int
    decision_time_ms: int
    chain_wait_ms: int
    setup_chain_wait_ms: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    model_cost_estimated_usd: str | None
    model_cost_reported_usd: str | None
    gas_used_negotiation: str
    gas_used_setup: str
    fee_wei_negotiation: str
    fee_wei_setup: str
    settled_quote_minor: str | None
    mandate_violations: int
    failure_class: Literal["model", "signing", "rpc", "execution", "none"] | None
    audit_complete: bool
    rpc_requests: int
    rpc_requests_by_method: dict[str, int]
    rpc_cost_estimated_usd: str | None
    #: The RPC price table entry's `last_verified` date, shown beside the estimate; null when the
    #: table does not price this deployment's provider.
    rpc_price_last_verified: str | None
    buyer_utility_minor: str | None
    seller_utility_minor: str | None
    captured_surplus_minor: str | None
    feasible: bool | None
    feasible_surplus_minor: str | None
    #: The captured surplus over the feasible surplus; undefined, and null, when the latter is 0.
    efficiency_ratio: str | None
