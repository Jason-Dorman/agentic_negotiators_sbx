"""Per-run metrics of spec section 11.2, with the RPC request counts and estimated RPC cost of
ADR-061.

Built in stage 2.5 of docs/build_plan.md; see docs/architecture.md section 3.2. `figures.py` holds
each figure as a pure function over records, `calculator.py` the row they make up and the three
shapes it leaves the server in. Per-batch metrics arrive with the evaluator in stage 6.
"""

from api.metrics.calculator import MetricsCalculator, usd
from api.metrics.figures import Feasibility, feasibility, mandate_violations

__all__ = ["Feasibility", "MetricsCalculator", "feasibility", "mandate_violations", "usd"]
