#!/bin/sh
# Integration tests (docs/test_strategy.md section 2) run against a database of their own so a
# failed run never leaves the development database in a state the next run inherits.
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE DATABASE ${POSTGRES_DB}_test OWNER ${POSTGRES_USER};
SQL
