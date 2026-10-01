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
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog
from fastapi import FastAPI

from agent.keys import KeyHolder, KeyRefError, RootKeyHolder
from agent.policy import DeterministicPolicy, Policy
from agent.routes import install_routes
from agent.service import AgentService, PolicyFactory
from agent.settings import AgentSettings
from agent.signing import SetupBounds
from agent.state import RunRegistry
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

    def deterministic() -> Policy:
        return DeterministicPolicy(validator)

    # Open/closed (docs/architecture.md section 9): stage 3 adds "model" here and nowhere else.
    policies: dict[str, PolicyFactory] = {"deterministic": deterministic}

    service = AgentService(
        role=settings.role,
        instance=settings.instance,
        keys=keys,
        signer_problem=problem,
        policies=policies,
        executor=TurnExecutor(validator, clock or SystemClock()),
        registry=RunRegistry(),
        setup_bounds=SetupBounds(settings.setup_gas_limit_max, settings.setup_max_cost_wei),
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
    log.info("started", policy_kinds=sorted(policies), signer_ok=keys is not None)
    return app
