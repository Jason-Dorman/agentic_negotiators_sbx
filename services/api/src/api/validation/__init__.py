"""Setup validation of spec section 3.1: the deployment against the chain, the agents, the run's
public configuration. Never feasibility, never a mandate.

Built in stage 2.4 of docs/build_plan.md; see docs/architecture.md section 3.2.
"""

from api.validation.validator import SUPPORTED_CHAINS, Check, SetupValidator, ValidationReport

__all__ = ["SUPPORTED_CHAINS", "Check", "SetupValidator", "ValidationReport"]
