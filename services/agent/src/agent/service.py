"""The use cases behind the agent internal API (docs/api_contract.md section 6).

The routes parse and authenticate; this module decides. Each method takes domain values and returns
the JSON-ready response the contract specifies, or raises one of `agent.errors` with the contract's
code. Nothing here knows about HTTP, and nothing here holds a key: signing goes through the run's
`RunSigner`, which the `KeyHolder` derived at provisioning.

Three rules shape it.

- **Idempotent for an identical request.** Provisioning a run twice with the same body, approving
  the same session twice, or asking for the same turn twice with the same observation returns the
  first answer. The same operation with different content is `idempotency_conflict` (provision,
  turn) or `session_mismatch` (approve-session), never a second, different signature.
- **The observation is checked against what this instance approved, before any policy is asked.**
  A turn whose run, role, address or session is not this instance's own, or whose session deadline
  has passed, is refused at the door, so a model is never paid to decide an action nothing could
  sign.
- **The mandate is injected here, not received.** A turn body that carries a `mandate` is refused:
  the backend sends a mandate to an agent once, at provisioning (docs/architecture.md section 5.1).

A model run gets its own policy at provisioning, from `model_policy_factory`: a client of the
instance's `ModelRuntime`, live or fixture, with a budget over the run's own ceilings, and the
system prompt rendered from the run's role and instructions. An instance whose model configuration
did not load still lists `model` — so a run that needs it fails validation with a reason, not on a
missing kind — reports `model_ok: false`, and refuses to provision a model run with
`503 dependency_unavailable`, as it does for its signer. Its client sits behind the instance's
outbound-context assertion, with the run's own key added to it: a request carrying anything private
to the instance is not sent, and the turn is answered `422 outbound_context_refused` (ADR-092).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from agent.consistency import contradictions
from agent.errors import (
    AddressMismatchError,
    DependencyUnavailableError,
    IdempotencyConflictError,
    InvalidStateError,
    KeyRefMismatchError,
    ObservationInconsistentError,
    RequestValidationError,
    SessionMismatchError,
)
from agent.keys import SCHEME, KeyDerivation, KeyHolder, RunSigner
from agent.model import ModelMode, ModelRuntime, RunModelLimits
from agent.observation import Observation, Role
from agent.policy import ModelPolicy, Policy
from agent.prompting import PromptTemplate
from agent.signing import (
    ApprovedSession,
    FeeTerms,
    SessionOpened,
    SetupBounds,
    approve_session,
    build_setup_approval,
)
from agent.state import (
    APPROVED,
    PROVISIONED,
    UNPROVISIONED,
    AnsweredTurn,
    Provisioning,
    RunRecord,
    RunRegistry,
)
from agent.turns import TurnExecutor
from negotiation_protocol import Address, json_sha256, validator_for

#: A run's policy, from its provisioning and its signer — which a model run's outbound-context
#: assertion asks whether a request carries the run's key (ADR-092). No policy signs with it.
PolicyFactory = Callable[[Provisioning, RunSigner], Policy]
_TURN_ENVELOPE: Final = ("turn", "deadline_at")


class AgentService:
    def __init__(
        self,
        *,
        role: Role,
        instance: str,
        keys: KeyHolder | None,
        signer_problem: str | None,
        policies: Mapping[str, PolicyFactory],
        executor: TurnExecutor,
        registry: RunRegistry,
        setup_bounds: SetupBounds,
        model_mode: ModelMode | None = None,
        model_ok: bool = False,
    ) -> None:
        self._role = role
        self._instance = instance
        self._keys = keys
        self._signer_problem = signer_problem
        self._policies = dict(policies)
        self._executor = executor
        self._registry = registry
        self._setup_bounds = setup_bounds
        self._model_mode = model_mode
        self._model_ok = model_ok

    # ----------------------------------------------------------------------------------
    # Health
    # ----------------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "role": self._role,
            "instance": self._instance,
            "policy_kinds": sorted(self._policies),
            "model_ok": self._model_ok,
            # ADR-088: `fixture` when this instance answers from canned responses, by its own
            # configuration; the backend records a model run on it as a fixture run.
            "model_mode": self._model_mode,
            "signer_ok": self._keys is not None,
        }

    # ----------------------------------------------------------------------------------
    # Provision
    # ----------------------------------------------------------------------------------

    def provision(
        self, run_id: UUID, request: Provisioning, expected_address: Address | None
    ) -> Mapping[str, Any]:
        if self._registry.is_released(run_id):
            raise InvalidStateError(
                f"run {run_id} was released on this instance",
                state="released",
                allowed_from=[UNPROVISIONED, PROVISIONED],
            )
        existing = self._registry.get(run_id)
        if existing is not None:
            return _reprovision(existing, request, expected_address)
        keys = self._require_signer()
        self._check_identity(request, keys)
        factory = self._policy_factory(request)
        derivation = KeyDerivation(
            chain_id=request.expected.chain_id, role=self._role, run_id=run_id
        )
        signer = keys.signer_for(derivation)
        policy = factory(request, signer)
        _check_address(signer.address, expected_address)
        response = {
            "provisioned": True,
            "my_address": str(signer.address),
            "key_derivation": derivation.to_json(),
            "policy_version": policy.version,
            "prompt_template_version": policy.prompt_template_version,
        }
        self._registry.add(RunRecord(run_id, request, policy, signer, response))
        return response

    def _require_signer(self) -> KeyHolder:
        if self._keys is None:
            raise DependencyUnavailableError(
                "this instance's signer is not available",
                {"dependency": "signer", "reason": self._signer_problem},
            )
        return self._keys

    def _check_identity(self, request: Provisioning, keys: KeyHolder) -> None:
        if request.key_ref != keys.key_ref:
            raise KeyRefMismatchError(
                "the key reference is not this instance's root",
                {"expected": keys.key_ref, "received": request.key_ref},
            )
        if request.role != self._role:
            raise RequestValidationError(
                "provisioned for the wrong role", {"role": f"this instance is the {self._role}"}
            )

    def _policy_factory(self, request: Provisioning) -> PolicyFactory:
        factory = self._policies.get(request.policy)
        if factory is None:
            available = ", ".join(sorted(self._policies))
            raise RequestValidationError(
                f"policy {request.policy!r} is not available on this instance",
                {"policy": f"one of: {available}"},
            )
        return factory

    # ----------------------------------------------------------------------------------
    # Approve session
    # ----------------------------------------------------------------------------------

    def approve_session(self, run_id: UUID, opened: SessionOpened) -> Mapping[str, Any]:
        record = self._registry.require(run_id, (PROVISIONED, APPROVED))
        if record.opened is None or record.approval is None:
            record.approval = approve_session(
                record.provisioning.expected, opened, self._role, record.signer.address
            )
            record.opened = opened
        elif opened != record.opened:
            raise SessionMismatchError(
                f"run {run_id} already approved a different session",
                {"fields": _changed(record.opened.to_json(), opened.to_json())},
            )
        return {"approved": True, "config_hash": str(record.approval.config_hash)}

    # ----------------------------------------------------------------------------------
    # Setup approval (ADR-040)
    # ----------------------------------------------------------------------------------

    def setup_approval(self, run_id: UUID, terms: FeeTerms) -> Mapping[str, Any]:
        record = self._registry.require(run_id, (PROVISIONED, APPROVED))
        approval = build_setup_approval(
            role=self._role,
            expected=record.provisioning.expected,
            allowance=record.provisioning.allowance,
            terms=terms,
            bounds=self._setup_bounds,
            signer=record.signer,
        )
        return approval.to_json()

    # ----------------------------------------------------------------------------------
    # Turn (ADR-012)
    # ----------------------------------------------------------------------------------

    async def turn(self, run_id: UUID, body: Mapping[str, Any]) -> Mapping[str, Any]:
        record = self._registry.require(run_id, (APPROVED,))
        turn, deadline = _turn_envelope(body)
        document = _observation_document(body, record.provisioning.mandate_document)
        observation_hash = json_sha256(document)
        async with record.turn_lock:
            answered = record.turns.get(turn)
            if answered is not None:
                if answered.observation_hash != observation_hash:
                    raise IdempotencyConflictError(
                        f"turn {turn} was already answered for a different observation"
                    )
                return answered.response
            approval = _approval(record)
            observation = _typed(document)
            self._check_observation(run_id, record, approval, observation)
            outcome = await self._executor.run(
                turn=turn,
                observation=observation,
                observation_hash=observation_hash,
                policy=record.policy,
                attempts=1 + record.provisioning.repair_attempts,
                approval=approval,
                signer=record.signer,
                still_open=lambda: not record.released,
                deadline=deadline,
            )
            response = outcome.to_json()
            record.turns[turn] = AnsweredTurn(observation_hash, response)
            return response

    def _check_observation(
        self,
        run_id: UUID,
        record: RunRecord,
        approval: ApprovedSession,
        observation: Observation,
    ) -> None:
        fields = _changed(
            {"run_id": str(run_id), "role": self._role, "my_address": str(record.signer.address)},
            {
                "run_id": str(observation.run_id),
                "role": observation.role,
                "my_address": str(observation.my_address),
            },
        )
        for name, difference in approval.mismatches_with(observation.session).items():
            fields[f"session.{name}"] = difference
        if fields:
            raise SessionMismatchError(
                "the observation is not of this instance's run and approved session",
                {"fields": fields},
            )
        if observation.chain_time >= approval.expires_at:
            raise InvalidStateError(
                f"the session deadline {approval.expires_at} has passed at chain time "
                f"{observation.chain_time}",
                state="session_deadline_passed",
                allowed_from=[APPROVED],
            )
        problems = contradictions(observation, approval)
        if problems:
            raise ObservationInconsistentError(
                "the observation contradicts itself or the approved session", problems
            )

    # ----------------------------------------------------------------------------------
    # Release
    # ----------------------------------------------------------------------------------

    def release(self, run_id: UUID) -> Mapping[str, Any]:
        self._registry.release(run_id)
        return {"released": True}


def _reprovision(
    record: RunRecord, request: Provisioning, expected_address: Address | None
) -> Mapping[str, Any]:
    if record.provisioning.fingerprint != request.fingerprint:
        raise IdempotencyConflictError(
            f"run {record.run_id} was provisioned on this instance with a different body"
        )
    _check_address(record.signer.address, expected_address)
    return record.provisioned_response


def _check_address(derived: Address, expected: Address | None) -> None:
    if expected is not None and expected != derived:
        raise AddressMismatchError(
            "this instance's derivation does not reproduce the stored address",
            {"expected": str(expected), "derived": str(derived), "scheme": SCHEME},
        )


def _approval(record: RunRecord) -> ApprovedSession:
    if record.approval is None:
        # `require(run_id, (APPROVED,))` has already ruled this out; the signer refuses it too.
        raise InvalidStateError(
            f"run {record.run_id} has no approved session",
            state=PROVISIONED,
            allowed_from=[APPROVED],
        )
    return record.approval


def _changed(expected: Mapping[str, Any], received: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: {"expected": expected[name], "received": received[name]}
        for name in expected
        if expected[name] != received[name]
    }


def _turn_envelope(body: Mapping[str, Any]) -> tuple[int, datetime]:
    """The turn number and the deadline by which this instance must answer (ADR-089)."""
    turn = body.get("turn")
    number = turn if isinstance(turn, int) and not isinstance(turn, bool) and turn >= 1 else None
    deadline = _iso_utc(body.get("deadline_at"))
    if number is None or deadline is None:
        problems = {}
        if number is None:
            problems["turn"] = "an integer of at least 1"
        if deadline is None:
            problems["deadline_at"] = "an ISO 8601 UTC timestamp ending in Z"
        raise RequestValidationError("the turn envelope is malformed", problems)
    return number, deadline


def _iso_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)
    except ValueError:
        return None


def model_policy_factory(
    runtime: ModelRuntime | None, problem: str | None, template: PromptTemplate | None
) -> PolicyFactory:
    """The `model` entry of the policy registry. Each run gets its own client and budget."""

    def factory(request: Provisioning, signer: RunSigner) -> Policy:
        if request.model_id is None or request.effort is None:
            raise RequestValidationError(
                "a model run names its model and its effort",
                {
                    name: "required for a model policy"
                    for name, value in (("model_id", request.model_id), ("effort", request.effort))
                    if value is None
                },
            )
        if runtime is None or template is None:
            raise DependencyUnavailableError(
                "this instance's model is not available",
                {"dependency": "model", "reason": problem or "not configured"},
            )
        limits = RunModelLimits(
            call_ceiling=request.model_call_ceiling,
            spend_ceiling_usd=request.model_spend_ceiling_usd,
            timeout_s=request.model_timeout_s,
        )
        return ModelPolicy(
            runtime.client_for(request.model_id, request.effort, limits, signer),
            template,
            role=request.role,
            instructions=str(request.mandate_document.get("instructions", "")),
        )

    return factory


def _observation_document(body: Mapping[str, Any], mandate: Mapping[str, Any]) -> dict[str, Any]:
    """The observation this instance decides on: the body less its envelope, plus its mandate."""
    if "mandate" in body:
        raise RequestValidationError(
            "a turn never carries a mandate",
            {"mandate": "sent once, at provisioning; this instance injects its own"},
        )
    document = {key: value for key, value in body.items() if key not in _TURN_ENVELOPE}
    document["mandate"] = dict(mandate)
    problems = schema_problems(document, "observation.v1.json")
    if problems:
        raise RequestValidationError("the observation does not match observation.v1.json", problems)
    return document


def _typed(document: Mapping[str, Any]) -> Observation:
    """The typed view. A `ProtocolValueError` here is a value the schema's loose patterns admitted.

    The error's own text quotes the value, so it is not returned; see `schema_problems`.
    """
    try:
        return Observation.from_json(document)
    except ValueError:
        raise RequestValidationError(
            "the observation holds a value its type refuses",
            {"observation": "a value is not in its type's canonical form or range"},
        ) from None


def schema_problems(instance: Any, schema: str) -> dict[str, str]:
    """Each failing location and the schema keyword it broke, never the offending value.

    A schema error message quotes the value, and in an observation or a mandate the value can be a
    private one. Error responses reach the backend, which logs them, so only the location and the
    rule are returned.
    """
    problems: dict[str, str] = {}
    for error in validator_for(schema).iter_errors(instance):
        path = "/".join(str(part) for part in error.absolute_path) or "<root>"
        problems.setdefault(path, f"fails {error.validator}")
    return problems
