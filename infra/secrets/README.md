# `infra/secrets`

Encrypted web3 keystore JSON for the Sepolia profile ([ADR-023](../../docs/decision_log.md)).
The directory is git-ignored except for this file, and the secret scan rejects keystore JSON
anywhere in the tree, so a file that lands here by accident is caught twice.

The keystore path reaches the key holder as a `keystore:` reference and the password reaches it
from `KEYSTORE_PASSWORD`. Neither is ever a value in the database, a log line, an SSE frame or
an export.

This exists from stage 0 rather than being retrofitted at stage 5, so the `keystore:` path
through the key holder is exercised by tests from stage 2 and the Sepolia deployment introduces
no new code path.

```sh
# Local profile: throwaway keys as `env:` refs, no password needed.
uv run --group tooling python infra/scripts/generate_keys.py --profile local

# Sepolia profile: encrypted keystores written here.
KEYSTORE_PASSWORD='…' uv run --group tooling python infra/scripts/generate_keys.py --profile sepolia
```

None of this is production custody, and the runbook says so.
