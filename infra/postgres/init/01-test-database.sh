#!/bin/sh
# Integration tests (docs/test_strategy.md section 2) run against a database of their own so a
# failed run never leaves the development database in a state the next run inherits.
#
# Both names derive from POSTGRES_DB, so a developer who renames the database in infra/.env
# gets a matching test database without editing anything here.
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE DATABASE ${POSTGRES_DB}_test OWNER ${POSTGRES_USER};
SQL
