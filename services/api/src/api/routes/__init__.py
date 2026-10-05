"""The HTTP and SSE surface of api_contract.md section 2. Nothing imports it.

Built in stage 2.5 of docs/build_plan.md; see docs/architecture.md section 3.2. `app.py` builds the
application from the services the composition root hands it; `runs.py`, `system.py` and `sse.py`
are the routes; `idempotency.py` and `operations.py` the plumbing every mutation route shares;
`models.py` the response shapes the OpenAPI document states; `errors.py` the error envelope.
"""

from api.routes.app import API_VERSION, create_app
from api.routes.idempotency import Idempotency
from api.routes.operations import OperationTracker
from api.routes.services import Services, Shutdown

__all__ = ["API_VERSION", "Idempotency", "OperationTracker", "Services", "Shutdown", "create_app"]
