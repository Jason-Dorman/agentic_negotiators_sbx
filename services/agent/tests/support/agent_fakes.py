"""Test doubles for the agent's protocols: fakes, not mocks (docs/test_strategy.md section 1).

`KeyedRunSigner` is a `RunSigner` over a key the test chooses — the EIP-712 fixture's participant
keys, so a signature can be compared with the fixture's — and it records every signature it makes,
which is how a test asserts that nothing was signed. `KeyedKeyHolder` hands one out per derivation.
`ScriptedPolicy` returns responses a test wrote, in order, for driving the turn executor through
refusals a real policy would never make. `FakeModelClient` does the same for `ModelPolicy`, one
`ModelResult` per call, and records every request it was given.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_observations import FIXTURE
from eth_account import Account
from pydantic import BaseModel

from agent.keys import KeyDerivation, RunSigner, SignedTransaction
from agent.model import ModelResult
from agent.observation import Observation, Role
from agent.policy import PolicyFailure, PolicyKind, PolicyResponse, Repair
from negotiation_protocol import Address, Digest

FIXTURE_KEYS: dict[Role, str] = {
    "buyer": FIXTURE["participants"]["buyer"]["private_key"],
    "seller": FIXTURE["participants"]["seller"]["private_key"],
}


class KeyedRunSigner:
    def __init__(self, key: str, derivation: KeyDerivation) -> None:
        self._account = Account.from_key(key)
        self._derivation = derivation
        self.digests_signed: list[bytes] = []
        self.transactions_signed: list[dict[str, Any]] = []

    @property
    def address(self) -> Address:
        return Address(self._account.address)

    @property
    def derivation(self) -> KeyDerivation:
        return self._derivation

    def sign_digest(self, digest: bytes) -> bytes:
        self.digests_signed.append(digest)
        return bytes(self._account.unsafe_sign_hash(digest).signature)

    def sign_transaction(self, transaction: Mapping[str, Any]) -> SignedTransaction:
        self.transactions_signed.append(dict(transaction))
        signed = self._account.sign_transaction(dict(transaction))
        return SignedTransaction(bytes(signed.raw_transaction), Digest(bytes(signed.hash)))

    def appears_in(self, text: str) -> bool:
        return bytes(self._account.key).hex() in text.lower()

    @property
    def signatures_made(self) -> int:
        return len(self.digests_signed) + len(self.transactions_signed)


class KeyedKeyHolder:
    """A `KeyHolder` that gives each role the fixture's key for it, whatever the run."""

    def __init__(self, key_ref: str = "env:BUYER_ROOT_KEY") -> None:
        self._key_ref = key_ref
        self.signers: list[KeyedRunSigner] = []

    @property
    def key_ref(self) -> str:
        return self._key_ref

    def signer_for(self, derivation: KeyDerivation) -> RunSigner:
        signer = KeyedRunSigner(FIXTURE_KEYS[derivation.role], derivation)
        self.signers.append(signer)
        return signer

    def appears_in(self, text: str) -> bool:
        """The fake has no root of its own: no text holds it."""
        return False

    @property
    def signatures_made(self) -> int:
        return sum(signer.signatures_made for signer in self.signers)


class ScriptedPolicy:
    """Returns the scripted responses in order, repeating the last one, and records its calls."""

    def __init__(self, *responses: Any) -> None:
        self._responses: Sequence[Any] = responses
        self.calls: list[tuple[Observation, Repair | None]] = []
        self.time_left: list[float | None] = []

    @property
    def kind(self) -> PolicyKind:
        return "deterministic"

    @property
    def version(self) -> str:
        return "scripted-0"

    @property
    def prompt_template_version(self) -> str | None:
        return None

    async def decide(
        self, observation: Observation, repair: Repair | None, *, time_left_s: float | None
    ) -> PolicyResponse | PolicyFailure:
        """A scripted `PolicyResponse` or `PolicyFailure` is returned as it is; anything else is
        the raw response of a computed one."""
        self.calls.append((observation, repair))
        self.time_left.append(time_left_s)
        index = min(len(self.calls), len(self._responses)) - 1
        response = self._responses[index]
        if isinstance(response, PolicyResponse | PolicyFailure):
            return response
        return PolicyResponse.computed(response)


class ModelCall:
    """One request a `FakeModelClient` was given, as the client would have sent it."""

    def __init__(
        self,
        system_prompt: str,
        observation: str,
        schema: type[BaseModel],
        repair: str | None,
        within_s: float | None,
    ) -> None:
        self.system_prompt = system_prompt
        self.observation = observation
        self.schema = schema
        self.repair = repair
        self.within_s = within_s


class FakeModelClient:
    """A `ModelClient` that returns the scripted `ModelResult`s in order and records each request.

    Running out of script is a test error, not a model outcome, so it raises.
    """

    def __init__(self, *results: ModelResult) -> None:
        self._results = list(results)
        self.calls: list[ModelCall] = []

    async def decide(
        self,
        system_prompt: str,
        observation: str,
        schema: type[BaseModel],
        *,
        repair: str | None = None,
        within_s: float | None = None,
    ) -> ModelResult:
        self.calls.append(ModelCall(system_prompt, observation, schema, repair, within_s))
        if len(self.calls) > len(self._results):
            raise AssertionError(f"the model was called {len(self.calls)} times; scripted fewer")
        return self._results[len(self.calls) - 1]
