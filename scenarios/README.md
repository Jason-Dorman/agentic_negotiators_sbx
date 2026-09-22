# `scenarios`

Manufactured scenario templates and evaluation populations, as JSON
([ADR-022](../docs/decision_log.md)). Every file here validates in CI against
`packages/protocol/schemas/scenario.v1.json`.

Templates arrive with the protocol schemas in stage 1; batch populations are generated from a
seed and committed with that seed so results are reproducible
([test_strategy.md](../docs/test_strategy.md) section 11).

All values in these files are manufactured experiment inputs. None describes a real asset,
a real counterparty or a real price.
