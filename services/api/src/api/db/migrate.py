"""Run the backend's migrations: `python -m api.db.migrate upgrade|downgrade [revision]`.

The migrations ship inside the `api` package (`api/db/migrations/`), and this module locates them
from the package rather than from an ini file in a checkout. That is what lets the backend image run
them at start-up in the local profile, and an operator run them by hand on a Sepolia host
(docs/data_model.md section 8), with nothing but the installed package and `DATABASE_URL`.

The URL is handed to Alembic as a config *attribute*, never through the ini parser, whose `%`
interpolation would corrupt a password containing a percent sign.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR: Final = Path(__file__).resolve().parent / "migrations"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["database_url"] = database_url
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(database_url), revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("direction", choices=("upgrade", "downgrade"))
    parser.add_argument("revision", nargs="?", help="default: head for upgrade, base for downgrade")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        sys.stderr.write("DATABASE_URL is not set (see infra/.env.example).\n")
        return 2

    if args.direction == "upgrade":
        upgrade(database_url, args.revision or "head")
    else:
        downgrade(database_url, args.revision or "base")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
