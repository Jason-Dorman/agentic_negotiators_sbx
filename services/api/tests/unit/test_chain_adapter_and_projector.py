"""The adapter's error translation and the projector's refusals, without a chain or a database.

The adapter's happy paths are exercised against Anvil by the integration suite; what is left here
is the translation of failures a healthy Anvil does not produce on demand.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

import pytest
from web3.exceptions import ContractLogicError, Web3RPCError

from api.chain import RpcUnavailableError, TransactionRejectedError, Web3ChainAdapter
from api.chain.adapter import _revert_data, _rpc_message
from api.db import NotFoundError, UnitOfWork
from api.projection import Projector, confirmation_threshold


def test_revert_data_is_read_from_hex_and_otherwise_empty() -> None:
    assert _revert_data(ContractLogicError("x", data="0x1234")) == b"\x12\x34"
    assert _revert_data(ContractLogicError("x", data="0xzz")) == b""
    assert _revert_data(ContractLogicError("x", data=None)) == b""


def test_an_rpc_refusal_keeps_the_nodes_message() -> None:
    error = Web3RPCError("x", rpc_response=cast("Any", {"error": {"message": "nonce too low"}}))
    assert _rpc_message(error) == "nonce too low"
    assert _rpc_message(Web3RPCError("plain")) == "plain"
    rejected = TransactionRejectedError("Transaction Already Imported")
    assert rejected.already_known
    assert not rejected.nonce_too_low


async def test_an_unreachable_rpc_is_one_error_whatever_the_call() -> None:
    adapter = Web3ChainAdapter("http://127.0.0.1:1", timeout_s=1)
    for call in (adapter.chain_id(), adapter.head(), adapter.finalized_number()):
        with pytest.raises(RpcUnavailableError, match="did not answer"):
            await call


@pytest.mark.parametrize("value", [0, -1, True, "2", 1.5])
def test_a_threshold_that_is_not_a_positive_integer_is_refused(value: Any) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        confirmation_threshold({"confirmation_threshold": value}, 1)


def test_a_missing_threshold_takes_the_default() -> None:
    assert confirmation_threshold({}, 2) == 2
    assert confirmation_threshold({"confirmation_threshold": 3}, 1) == 3


class _NoRuns:
    class _Runs:
        async def get(self, run_id: uuid.UUID) -> None:
            return None

    def unit_of_work(self) -> AbstractAsyncContextManager[UnitOfWork]:
        @asynccontextmanager
        async def scope() -> AsyncIterator[UnitOfWork]:
            uow = cast("Any", type("Uow", (), {"runs": self._Runs()})())
            yield uow

        return scope()


async def test_projecting_an_unknown_run_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        await Projector(_NoRuns()).project(uuid.uuid4())
