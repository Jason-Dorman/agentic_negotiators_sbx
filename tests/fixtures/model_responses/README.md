# Canned model responses

Fixture scripts for the model policy's fixture mode ([ADR-088](../../../docs/decision_log.md)). An
agent instance started with `AGENT_MODEL_FIXTURES` naming one of these directories answers every
model call of its runs from its role's script, `buyer.json` or `seller.json`, in call order, through
the run's real budget guard, validator and signer. A run that uses them is recorded as `fixture`
and labelled so on every surface: it is test evidence of the plumbing, never of a model's
behaviour (docs/test_strategy.md section 11).

| Directory | What it drives |
|---|---|
| `default-overlap-settles` | `default-overlap` to a settlement at 94: buyer 90, seller 98, buyer 94, seller accepts |
| `a04-seller-below-floor` | A04: the seller proposes 85 against its floor of 90, twice, and the run aborts with reason 2 |
| `a12-isolation` | A12, on the isolation suite's mandates (buyer bound 97.531246, seller floor 88.642317): buyer 80; seller 85.432109 refused below its floor, repaired to 104; buyer 99.123457 refused above its bound, repaired to 93; seller accepts. Each side has one refused attempt, so each has private feedback the other must never see |
| `a12-injection` | A12's prompt-injection run: a buyer obeying instructions that try to change the rules — a fourth decision shape refused, an in-mandate 82 whose explanation reveals its bound, an accept above its bound refused, then a walk-away |
| `spend-ceiling` | With a $0.40 spend ceiling and `claude-sonnet-5-5`, the buyer's third call is refused and the run aborts with reason 3 (at another model's prices, another call) |

The script format is `agent.model.fixtures`: each entry is an `answer` (a decision envelope, whose
string `"{{active_offer.offer_hash}}"` is filled from the observation), a `text`, or a `failure`,
with the `usage` the provider would report, and an optional `delay_s`, bounded like a live call by
the run's `model_timeout_s` and the turn's deadline. The usage figures are invented, shaped like a real
run's: the first call of a run writes the system prompt to the cache and later calls read it.
