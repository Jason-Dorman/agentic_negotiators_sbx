## What and why

<!-- What changed, and the reason. Not a restatement of the diff. -->

## Requirements served

<!-- FR-…, NFR-…, A… from docs/prd.md and docs/test_strategy.md. -->

## Decision record

<!-- ADR number if this changes protocol, API, data model, a PRD requirement or a stated
     assumption. "None required" is an acceptable answer; silence is not. -->

## How it was verified

<!-- Commands run and what they showed. -->

## Review checklist (docs/contributing.md section 5)

**Design**
- [ ] One responsibility per new class or function, in the module that owns it
- [ ] Dependencies on protocols, injected at the composition root
- [ ] No new module-level state, control flags, or `Manager`/`Helper` classes
- [ ] Complexity under 10, or justified in a comment
- [ ] No duplicated logic, no feature envy across module boundaries

**Correctness for this domain**
- [ ] Amounts are integers in minor units end to end
- [ ] Chain time, not wall-clock, for any expiry decision
- [ ] No path silently alters a model-proposed price
- [ ] Every signed message is built from validated state, never from model-supplied bytes
- [ ] New chain reads filter on `canonical = true` through the repository
- [ ] Persist before broadcast; rebroadcast never requests a new decision
- [ ] New failure paths land in a distinguishable state and never look like an economic outcome

**Privacy**
- [ ] No new field crosses the agent boundary unless it is in the observation allowlist
- [ ] No mandate, feedback, prompt, key or credential can reach logs, SSE, the default export
      or the opposing agent
- [ ] Private routes require the reveal header and are logged

**Tests and docs**
- [ ] Tests exist for the change and would fail without it
- [ ] Fixture-based runs are labelled as fixtures
- [ ] Docs updated in the same PR; ADR present if required
- [ ] Runbook updated if this introduces an operational procedure
