# `scenarios`

Manufactured scenario templates and evaluation populations, as JSON
([ADR-022](../docs/decision_log.md)). Every file here validates in CI against
`packages/protocol/schemas/scenario.v1.json`, and its `scenario_id` must match its file name because
the loader upserts by that id.

| File | What it is |
|---|---|
| `default-overlap.json` | The system spec's section 2.3 defaults. Buyer limit 100 mUSD, seller floor 90 mUSD, 10 mASSET, eight offers, 1,800 s. The feasible interval is 90 to 100 mUSD and **only the evaluator may compute it** |
| `infeasible-clone.json` | The same scenario with the seller's floor at 105 mUSD. No price satisfies both mandates, so a walk-away or an expiry is the correct outcome — acceptance A03 |

Batch populations are generated from a seed and committed with that seed so results are reproducible
([test_strategy.md](../docs/test_strategy.md) section 11).

## What these files are not

All values here are manufactured experiment inputs. None describes a real asset, a real counterparty
or a real price.

A scenario file holds **both** mandates, so it is private experimental input as a whole:
`GET /v1/scenarios` lists only the public fields, and the full file requires the observer reveal
header. Two things are deliberately absent from the schema:

- **Feasibility.** No field states or implies whether the two mandates overlap. That is computed by
  the evaluator alone and is never exposed on a setup route.
- **`confirmation_threshold`.** It is a property of the chain a run executes on — 1 locally, 2 on
  Sepolia — not of the economics being tested. A scenario that carried one would silently change the
  meaning of a run moved between profiles.

The mandate `instructions` are the agent's private guidance and cannot override protocol rules or
the output schema; the prompt template is what enforces that ([ADR-029](../docs/decision_log.md)).
