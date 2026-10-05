"""`python -m api`: serve the operator API (docs/runbook.md section 8).

Configuration is the environment's (`infra/.env.example`). A configuration error is one line on
stderr naming each variable and the rule it broke, never a value, and exit status 2. Uvicorn's
access log is off: the application writes its own line per request, which never carries a body.
"""

from __future__ import annotations

import sys

from api.config import SettingsError, load_api_settings
from api.logs import configure_logging
from api.main import create_app_from_environment, make_server
from api.routes import Shutdown


def main() -> None:
    shutdown = Shutdown()
    try:
        settings = load_api_settings()
        app = create_app_from_environment(shutdown)
    except SettingsError as error:
        print(error, file=sys.stderr)  # noqa: T201  reason: a start-up refusal, before logging
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level)
    # A lifespan that refuses to start — a StartupError, logged — ends the process with uvicorn's
    # exit status 3.
    make_server(app, shutdown, host=settings.api_host, port=settings.api_port).run()


if __name__ == "__main__":
    main()
