"""`python -m agent`: run one agent instance (docs/runbook.md section 3).

Configuration comes from `AGENT_*` variables (`agent.settings`); the root secret from whatever
`AGENT_ROOT_KEY_REF` names. Uvicorn's access log is off: the routes write their own line per
request, which carries the run id and never a body.
"""

from __future__ import annotations

import os
import sys

import uvicorn

from agent.logs import configure_logging
from agent.main import create_app
from agent.settings import SettingsError, load_settings


def main() -> None:
    try:
        settings = load_settings()
    except SettingsError as error:
        # The one line before logging exists, and it quotes no value (load_settings).
        print(error, file=sys.stderr)  # noqa: T201  reason: a start-up refusal, before logging
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, instance=settings.instance, role=settings.role)
    app = create_app(settings, environ=os.environ)
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
