"""The composition root: settings in, one FastAPI application out.

Concrete classes are chosen here and nowhere else (docs/contributing.md section 2.1): the key
holder, the validator, the policies on offer, the turn executor's clock, the run registry. Tests
build the same application through `create_app` with their own environment, which is why the
environment is a parameter rather than read from `os.environ` inside.

The root secret is resolved once, at start-up. If it cannot be — a variable unset, a keystore that
will not decrypt — the service still starts, reports `signer_ok: false` from its health route with
the reason in its log, and refuses to provision with `503 dependency_unavailable`. That keeps "the
agent is down" and "the agent's key is misconfigured" distinguishable to the setup validator, which
is the difference an operator needs to see (docs/architecture.md goal 4).

The model is loaded the same way. An instance configured for one — a key reference, or a fixture
directory (ADR-088) — offers the `model` policy; if its price table, its key, its fixture script or
its prompt template cannot be read, it still starts, reports `model_ok: false` with the reason in
its log, and refuses to provision a model run with `503 dependency_unavailable`.
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog
from fastapi import FastAPI

from agent.budget import DEFAULT_MODEL_PRICE_TABLE, BudgetGuard, ModelPriceTable, PriceTableError
from agent.keys import KeyHolder, KeyRefError, RootKeyHolder
from agent.model import (
    AnthropicModelClient,
    ClientMaker,
    DecisionEnvelope,
    FixtureError,
    FixtureModelClient,
    ModelCallConfig,
    ModelClient,
    ModelKeyError,
    ModelMode,
    ModelRuntime,
    anthropic_sdk,
    load_fixture_scripts,
    resolve_model_key,
)
from agent.policy import DeterministicPolicy, Policy
from agent.prompting import PromptTemplate, PromptTemplateError
from agent.routes import install_routes
from agent.service import AgentService, PolicyFactory, model_policy_factory
from agent.settings import AgentSettings
from agent.signing import SetupBounds
from agent.state import Provisioning, RunRegistry
from agent.turns import Clock, SystemClock, TurnExecutor
from agent.validation import MandateValidator


def load_key_holder(
    key_ref: str, environ: Mapping[str, str]
) -> tuple[KeyHolder | None, str | None]:
    """The instance's key holder, or the reason there is none. Never raises for a bad reference."""
    try:
        return RootKeyHolder.from_ref(key_ref, environ), None
    except KeyRefError as error:
        return None, str(error)


def configured_model_mode(settings: AgentSettings) -> ModelMode | None:
    if settings.model_fixtures is not None:
        return "fixture"
    return "live" if settings.model_key_ref is not None else None


def load_model_runtime(
    settings: AgentSettings, environ: Mapping[str, str]
) -> tuple[ModelRuntime | None, str | None]:
    """The instance's model runtime, or the reason there is none. Never raises for a bad
    configuration; None and None when no model is configured at all."""
    mode = configured_model_mode(settings)
    if mode is None:
        return None, None
    try:
        prices = ModelPriceTable.load(settings.model_price_table or DEFAULT_MODEL_PRICE_TABLE)
        make: ClientMaker
        if settings.model_fixtures is not None:
            script = load_fixture_scripts(settings.model_fixtures)[settings.role]

            def make(config: ModelCallConfig, guard: BudgetGuard) -> ModelClient:
                return FixtureModelClient(script, config, guard)

        else:
            assert settings.model_key_ref is not None  # mode is live
            sdk = anthropic_sdk(resolve_model_key(settings.model_key_ref, environ))

            def make(config: ModelCallConfig, guard: BudgetGuard) -> ModelClient:
                return AnthropicModelClient(sdk, config, guard)

    except (PriceTableError, FixtureError, ModelKeyError) as error:
        return None, str(error)
    runtime = ModelRuntime(
        mode, prices, settings.allow_unknown_price, settings.model_max_tokens, make
    )
    return runtime, None


def create_app(
    settings: AgentSettings,
    *,
    environ: Mapping[str, str],
    clock: Clock | None = None,
) -> FastAPI:
    keys, problem = load_key_holder(settings.root_key_ref, environ)
    log = structlog.get_logger(component="agent.main")
    if problem is not None:
        log.error("signer_unavailable", key_ref=settings.root_key_ref, reason=problem)

    validator = MandateValidator()

    def deterministic(request: Provisioning) -> Policy:
        return DeterministicPolicy(validator)

    # Open/closed (docs/architecture.md section 9): a policy is an entry here and nowhere else.
    policies: dict[str, PolicyFactory] = {"deterministic": deterministic}
    model_mode = configured_model_mode(settings)
    runtime, model_problem = load_model_runtime(settings, environ)
    template: PromptTemplate | None = None
    if runtime is not None:
        try:
            template = PromptTemplate.load(DecisionEnvelope)
        except PromptTemplateError as error:
            runtime, model_problem = None, str(error)
    if model_mode is not None:
        policies["model"] = model_policy_factory(runtime, model_problem, template)
        if model_problem is not None:
            log.error("model_unavailable", model_mode=model_mode, reason=model_problem)

    service = AgentService(
        role=settings.role,
        instance=settings.instance,
        keys=keys,
        signer_problem=problem,
        policies=policies,
        executor=TurnExecutor(validator, clock or SystemClock()),
        registry=RunRegistry(),
        setup_bounds=SetupBounds(settings.setup_gas_limit_max, settings.setup_max_cost_wei),
        model_mode=model_mode,
        model_ok=runtime is not None,
    )
    # The interactive docs and the OpenAPI document would be unauthenticated routes into the agent.
    # No trailing-slash redirects: a redirect is an answer given before authentication, and the
    # backend's MAC covers the exact path it signs.
    app = FastAPI(
        title="negotiation-agent",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    install_routes(app, service, settings.shared_secret.get_secret_value().encode("utf-8"))
    log.info(
        "started",
        policy_kinds=sorted(policies),
        signer_ok=keys is not None,
        model_mode=model_mode,
        model_ok=runtime is not None,
    )
    return app
