"""Setup validation, spec section 3.1 and `POST /v1/runs/{id}/validate` (api_contract section 2.2).

Every check is reported, passing or not, so an operator sees the whole picture at once. The report
never says whether a scenario is feasible and never holds a mandate value: it is built from public
chain state, the deployment manifest, the agents' health, and the run's public configuration.

The chain is checked against the manifest rather than trusted (spec section 8): the chain ID must be
the manifest's and one of the two this sandbox runs on, and the runtime bytecode at each of the
three addresses must hash to the manifest's `code_hashes`. A run whose `confirmation_threshold` is
not an integer of at least 1 is refused here, before anything is recorded under it (ADR-059).

The report also says how the run's model decisions will be made (ADR-088): `fixture` when an agent
that runs a party's model policy reports `model_mode: "fixture"` in its health — canned responses
by that instance's own configuration — and `live` otherwise. The controller records it as
`runs.mode`, so every surface labels a canned run as one. One fixture party is enough: a run with
any canned decision is not evidence of autonomous behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from eth_utils.crypto import keccak

from api.agent_client import AgentClient, AgentError
from api.chain import ChainAdapter, RpcUnavailableError
from api.config import InvalidRunConfigError, confirmation_threshold
from api.db.enums import Party, PolicyKind, RunMode
from api.db.protocols import Transactions
from api.db.records import DeploymentRecord, RunRecord, WalletRecord
from negotiation_protocol import Address, Digest

#: Anvil and Sepolia (spec section 8). Any other chain is refused.
SUPPORTED_CHAINS: Final = frozenset({31337, 11155111})


@dataclass(frozen=True, slots=True)
class Check:
    check: str
    ok: bool
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {"check": self.check, "ok": self.ok}
        if self.detail is not None:
            document["detail"] = self.detail
        return document


@dataclass(frozen=True, slots=True)
class ValidationReport:
    checks: tuple[Check, ...]
    #: ADR-088: how the run's model decisions will be made, from the agents' own health.
    mode: RunMode = RunMode.LIVE

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [check.to_json() for check in self.checks]}


class SetupValidator:
    def __init__(
        self,
        transactions: Transactions,
        chain: ChainAdapter,
        agents: Mapping[Party, AgentClient],
        deployment: DeploymentRecord,
        *,
        default_threshold: int,
    ) -> None:
        self._transactions = transactions
        self._chain = chain
        self._agents = agents
        self._deployment = deployment
        self._default_threshold = default_threshold

    async def validate(self, run: RunRecord) -> ValidationReport:
        async with self._transactions.unit_of_work() as uow:
            wallets = {wallet.party: wallet for wallet in await uow.wallets.list_for_run(run.id)}
        checks = [self._deployment_check(run), self._threshold_check(run)]
        checks += await self._chain_checks()
        fixture = False
        for party in (Party.BUYER, Party.SELLER):
            agent_checks, canned = await self._agent_checks(run, party)
            checks += agent_checks
            fixture = fixture or canned
            checks.append(_wallet_check(party, wallets.get(party)))
        return ValidationReport(tuple(checks), RunMode.FIXTURE if fixture else RunMode.LIVE)

    def _deployment_check(self, run: RunRecord) -> Check:
        expected = self._deployment.deployment_id
        return Check("deployment", run.deployment_id == expected, expected)

    def _threshold_check(self, run: RunRecord) -> Check:
        try:
            threshold = confirmation_threshold(run.public_config, self._default_threshold)
        except InvalidRunConfigError as error:
            return Check("confirmation_threshold", False, str(error))
        return Check("confirmation_threshold", True, str(threshold))

    async def _chain_checks(self) -> list[Check]:
        try:
            head = await self._chain.head()
        except RpcUnavailableError as error:
            return [Check("rpc", False, str(error))]
        checks = [Check("rpc", True, f"latest block {head.number}")]
        try:
            checks.append(await self._chain_id_check())
            checks += [await self._code_check(name) for name in _CODE_ADDRESSES]
            checks.append(
                await self._eth_check("relay", self._deployment.relay_address, head.number)
            )
            checks.append(
                await self._eth_check("operator", self._deployment.operator_address, head.number)
            )
        except RpcUnavailableError as error:
            checks.append(Check("rpc", False, str(error)))
        return checks

    async def _chain_id_check(self) -> Check:
        actual = await self._chain.chain_id()
        ok = actual == self._deployment.chain_id and actual in SUPPORTED_CHAINS
        return Check("chain_id", ok, str(actual))

    async def _code_check(self, name: str) -> Check:
        address = Address(getattr(self._deployment, _CODE_ADDRESSES[name]))
        expected = self._deployment.code_hashes.get(name)
        actual = Digest(keccak(await self._chain.code(address)))
        ok = expected is not None and Digest(str(expected)) == actual
        return Check(f"{name}_code_hash", ok, None if ok else "does not match the manifest")

    async def _eth_check(self, name: str, address: Address, block: int) -> Check:
        balance = await self._chain.eth_balance(address, block)
        return Check(f"{name}_eth_balance", balance > 0, f"{balance} wei")

    async def _agent_checks(self, run: RunRecord, party: Party) -> tuple[list[Check], bool]:
        """The party's agent checks, and whether its model policy answers from fixtures."""
        name = f"{party.value}_agent"
        try:
            health = await self._agents[party].health()
        except AgentError as error:
            return [Check(name, False, f"unreachable: {type(error).__name__}")], False
        policy = run.buyer_policy if party == Party.BUYER else run.seller_policy
        problems = []
        if health.role != party.value:
            problems.append(f"serves the {health.role}")
        if not health.signer_ok:
            problems.append("its signer did not load")
        if policy.value not in health.policy_kinds:
            problems.append(f"cannot run a {policy.value} policy")
        checks = [Check(name, not problems, "; ".join(problems) or health.instance)]
        if policy != PolicyKind.MODEL:
            return checks, False
        checks.append(Check(f"{name}_model_available", health.model_ok, health.model_mode))
        return checks, health.model_mode == "fixture"


#: The three contracts whose bytecode is checked, and where each one's address is recorded.
_CODE_ADDRESSES: Final = {
    "exchange": "exchange_address",
    "base_token": "base_token_address",
    "quote_token": "quote_token_address",
}


def _wallet_check(party: Party, wallet: WalletRecord | None) -> Check:
    """Provisioned: the agent derived this run's address (ADR-039). Its balances are zero until the
    controller funds it on start, which is not a fault."""
    if wallet is None:
        return Check(f"{party.value}_wallet", False, "not provisioned")
    return Check(f"{party.value}_wallet", True, str(wallet.address))
