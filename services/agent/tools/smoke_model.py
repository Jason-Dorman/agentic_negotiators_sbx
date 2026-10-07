"""One real model call through the agent's client: `make smoke-model` (stage 3.1).

Opt-in and never a CI gate: it spends real money, about a cent, and needs a key. It shows that the
request the client builds — structured output, adaptive thinking, the effort, the cache breakpoint,
no retries, no fallbacks — is one the provider accepts, and that usage and both costs come back.

The key is read through a reference, as the agent reads it (ADR-086):

    BUYER_ANTHROPIC_API_KEY=sk-ant-… make smoke-model KEY_REF=env:BUYER_ANTHROPIC_API_KEY

`max_tokens` is 2,000 rather than the agent's 16,000, so whatever the model does the call cannot
cost more than about two cents on the default model, and about six on `claude-opus-5`, the dearest
in the table; the spend ceiling is $0.10, above every priced model's bound, and the call ceiling
one. Nothing private is printed: the outcome, the stop reason, the usage, the costs, the request id
and the latency.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal

from agent.budget import BudgetGuard, BudgetLimits, ModelPriceTable
from agent.model import (
    AnthropicModelClient,
    DecisionEnvelope,
    ModelCallConfig,
    ModelKeyError,
    ModelOutcome,
    anthropic_sdk,
    resolve_model_key,
)

SYSTEM_PROMPT = (
    "You are testing a negotiation agent's output format. Answer with one decision envelope. "
    'For this test, the decision is to walk away with the reason "terms_unacceptable".'
)
OBSERVATION = '{"test": "smoke", "expected": "walk_away"}'


async def main(arguments: argparse.Namespace) -> int:
    try:
        key = resolve_model_key(arguments.key_ref, os.environ)
    except ModelKeyError as error:
        print(f"smoke-model: {error}", file=sys.stderr)
        return 2
    price = ModelPriceTable.load().price(arguments.model)
    guard = BudgetGuard(
        BudgetLimits(call_ceiling=1, spend_ceiling_usd=Decimal("0.10")),
        price,
        allow_unknown_price=False,
    )
    config = ModelCallConfig(arguments.model, arguments.effort, max_tokens=2_000, timeout_s=45)
    client = AnthropicModelClient(anthropic_sdk(key), config, guard)
    result = await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope)

    print(f"outcome            {result.outcome}")
    print(f"model              {result.model_id} (served by {result.served_model})")
    print(f"stop reason        {result.stop_reason}")
    print(f"usage              {None if result.usage is None else result.usage.as_record()}")
    print(f"estimated cost     USD {result.cost_estimated_usd}")
    print(f"reported cost      USD {result.cost_reported_usd}")
    print(f"request id         {result.request_id}")
    print(f"latency            {result.latency_ms} ms")
    if result.provider_error is not None:
        print(
            f"provider error     {result.provider_error.status} {result.provider_error.error_type}"
        )
    if result.budget_refusal is not None:
        print(f"budget refusal     {result.budget_refusal.reason}: {result.budget_refusal.detail}")
    return 0 if result.outcome is ModelOutcome.DECIDED else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-ref", required=True, help="env:NAME, the variable holding the key")
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--effort", default="low")
    sys.exit(asyncio.run(main(parser.parse_args())))
