"""The reconstruction tool, against a real chain and against the fixture (acceptance A15).

Two layers, for two different questions.

The no-chain tests ask whether the calldata-to-digest path is right, using the committed EIP-712
fixture: given the struct as web3 would decode it, does the tool recompute the digest the four
languages already agree on? That is checkable without a chain and it is where a field-order mistake
would live.

The integration tests ask the question A15 actually poses: with the application database absent and
nothing but an RPC endpoint and a manifest, can the economic outcome of a run be established? They
drive a real negotiation on a real Anvil through the real deploy script, then reconstruct it — and
they include the negative cases, because a tool that says "reconstructed" about a session that never
happened would be worse than no tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import ANVIL_KEYS, TEST_MANIFEST_DIR, run_deploy_script
from negotiation_driver import NegotiationDriver
from reconstruct import TERMINAL_OUTCOMES, Reconstruction, Reconstructor
from web3 import Web3

from negotiation_protocol import load_fixture, to_bytes32, to_hex32, validate

FIXTURE = load_fixture("eip712.v1.json")

#: The checks a reconstruction of each outcome must actually have performed.
#:
#: `assert not failures` on its own is satisfied by a tool that stopped checking: an adversarial
#: review pointed out that 17 of the 20 checks could be deleted and these tests would stay green.
#: Naming the expected set turns "nothing failed" into "these ran and none failed".
IDENTITY_CHECKS = frozenset(
    {
        "chain_id",
        "exchange_code_hash",
        "base_token_code_hash",
        "quote_token_code_hash",
        "session_opened",
        "config_hash",
        "session_base_token",
        "session_quote_token",
    }
)

AUTHORITY_CHECKS = frozenset(
    {
        "signatures_recover_to_the_named_actor",
        "signatures_are_canonical",
        "the_relay_signed_nothing",
        "sequence_chain",
        "terminal_event",
    }
)

SETTLEMENT_CHECKS = frozenset(
    {
        "settlement_amount_was_signed",
        "settlement_event_matches_the_signed_amount",
        "settlement_quote_leg",
        "settlement_base_leg",
        "settlement_moved_nothing_else",
        "settlement_touched_only_manifest_contracts",
        "settled_digest_was_a_recorded_offer",
        "balance_deltas_match_the_settlement",
    }
)

SETTLED_CHECKS = IDENTITY_CHECKS | AUTHORITY_CHECKS | SETTLEMENT_CHECKS
UNSETTLED_CHECKS = IDENTITY_CHECKS | AUTHORITY_CHECKS | {"no_settlement_moved_no_tokens"}


def _assert_all_passed(result: object, expected: frozenset[str] | set[str]) -> None:
    """Every named check ran, and nothing that ran failed."""
    checks = result.checks  # type: ignore[attr-defined]  # reason: Reconstruction, kept loose to avoid importing for a helper
    failures = [c for c in checks if not c.ok]
    assert not failures, "\n".join(f"{c.check}: {c.detail}" for c in failures)

    ran = {c.check for c in checks}
    missing = set(expected) - ran
    assert not missing, f"these checks did not run: {sorted(missing)}"


# ---------------------------------------------------------------------------------------
# No chain needed
# ---------------------------------------------------------------------------------------


def _fixture_manifest() -> dict[str, Any]:
    """A manifest describing the deployment the EIP-712 fixture was computed for."""
    return {
        "chain_id": FIXTURE["domain"]["chain_id"],
        "exchange_address": FIXTURE["domain"]["verifying_contract"],
        "base_token_address": FIXTURE["deployment"]["base_token"],
        "quote_token_address": FIXTURE["deployment"]["quote_token"],
        "operator_address": FIXTURE["deployment"]["operator"],
        "relay_address": FIXTURE["deployment"]["operator"],
        "code_hashes": {
            "exchange": "0x" + "00" * 32,
            "base_token": "0x" + "00" * 32,
            "quote_token": "0x" + "00" * 32,
        },
        "start_block": 0,
    }


def _reconstructor_without_a_chain() -> Reconstructor:
    # `Web3()` with no provider builds contract objects and encodes fine; only a call would need a
    # connection, and none of the assertions below make one.
    return Reconstructor(Web3(), _fixture_manifest())


class TestDigestsFromDecodedCalldata:
    """The path a reconstruction takes: decoded struct in, digest out."""

    def test_an_offer_digest_matches_the_fixture(self) -> None:
        message = FIXTURE["messages"]["offer"]["message"]
        decoded = {
            "offer": {
                "sessionId": to_bytes32(message["session_id"]),
                "configHash": to_bytes32(message["config_hash"]),
                "sequence": message["sequence"],
                "proposer": message["proposer"],
                "quoteAmount": int(message["quote_amount"]),
                "validUntil": message["valid_until"],
            }
        }
        _, digest = _reconstructor_without_a_chain()._digest_for("recordOffer", decoded)
        assert "0x" + digest.hex() == FIXTURE["messages"]["offer"]["digest"]

    def test_an_accept_digest_matches_the_fixture(self) -> None:
        message = FIXTURE["messages"]["accept"]["message"]
        decoded = {
            "acceptance": {
                "sessionId": to_bytes32(message["session_id"]),
                "configHash": to_bytes32(message["config_hash"]),
                "sequence": message["sequence"],
                "actor": message["actor"],
                "offerHash": to_bytes32(message["offer_hash"]),
            }
        }
        _, digest = _reconstructor_without_a_chain()._digest_for("acceptAndSettle", decoded)
        assert "0x" + digest.hex() == FIXTURE["messages"]["accept"]["digest"]

    def test_a_close_digest_matches_the_fixture(self) -> None:
        message = FIXTURE["messages"]["close"]["message"]
        decoded = {
            "closure": {
                "sessionId": to_bytes32(message["session_id"]),
                "configHash": to_bytes32(message["config_hash"]),
                "sequence": message["sequence"],
                "actor": message["actor"],
                "reason": message["reason"],
            }
        }
        _, digest = _reconstructor_without_a_chain()._digest_for("closeSession", decoded)
        assert "0x" + digest.hex() == FIXTURE["messages"]["close"]["digest"]

    def test_the_rebuilt_message_uses_the_solidity_field_names(self) -> None:
        # The export's `signed_actions[].typed_message` is this structure, and a viewer recomputes
        # the digest from it with viem. camelCase there, snake_case in the fixture: the conversion
        # is the one this project makes, and it is checked rather than assumed.
        message = FIXTURE["messages"]["offer"]["message"]
        decoded = {
            "offer": {
                "sessionId": to_bytes32(message["session_id"]),
                "configHash": to_bytes32(message["config_hash"]),
                "sequence": message["sequence"],
                "proposer": message["proposer"],
                "quoteAmount": int(message["quote_amount"]),
                "validUntil": message["valid_until"],
            }
        }
        rebuilt, _ = _reconstructor_without_a_chain()._digest_for("recordOffer", decoded)
        assert set(rebuilt) == {
            "sessionId",
            "configHash",
            "sequence",
            "proposer",
            "quoteAmount",
            "validUntil",
        }
        # An amount leaves as a string, as it does everywhere else in JSON (CLAUDE.md).
        assert rebuilt["quoteAmount"] == message["quote_amount"]


class TestReconstructionRecord:
    def test_one_failed_check_fails_the_reconstruction(self) -> None:
        # `ok` is a conjunction on purpose. A tool that reported a partial reconstruction as a
        # success would let an unverifiable settlement through as evidence.
        result = Reconstruction(session_id="0x" + "11" * 32)
        result.record("first", True, "fine")
        assert result.ok
        result.record("second", False, "not fine")
        assert not result.ok

    def test_a_reconstruction_with_no_checks_is_not_evidence_of_anything(self) -> None:
        """An empty check list must not report success.

        `all([])` is True, so this asserted the opposite until an adversarial review pointed out
        what that means: a tool that crashed before recording anything, or that a future edit
        stopped calling the check methods on, would print RECONSTRUCTED and exit 0. "The chain does
        not support this settlement" and "the tool never looked" must not share an answer.
        """
        empty = Reconstruction(session_id="0x" + "11" * 32)
        assert not empty.ok
        assert empty.as_dict()["checks"] == []
        assert empty.as_dict()["ok"] is False

        # One passing check is enough to be a verdict; the guard is about emptiness, not a quorum.
        empty.record("something", True, "ran")
        assert empty.ok

    def test_every_terminal_event_maps_to_an_outcome(self) -> None:
        # docs/data_model.md section 5. The four terminal events and the four outcomes; a missing
        # entry would make an outcome unreadable from the chain.
        assert TERMINAL_OUTCOMES == {
            "SettlementCompleted": "settled",
            "SessionClosed": "closed",
            "SessionExpired": "expired",
            "SessionAborted": "aborted",
        }


# ---------------------------------------------------------------------------------------
# Against a real chain
# ---------------------------------------------------------------------------------------


@pytest.fixture
def w3(anvil_rpc: str) -> Web3:
    return Web3(Web3.HTTPProvider(anvil_rpc))


@pytest.fixture
def driver(w3: Web3, deployment: dict[str, Any]) -> NegotiationDriver:
    return NegotiationDriver(w3, deployment, ANVIL_KEYS)


@pytest.mark.integration
class TestAgainstAChain:
    def test_the_manifest_the_deploy_script_wrote_validates(
        self, deployment: dict[str, Any]
    ) -> None:
        manifest = {key: value for key, value in deployment.items() if not key.startswith("_")}
        validate(manifest, "deployment_manifest.v1.json")

    def test_a_settlement_reconstructs_from_chain_data_alone(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver
    ) -> None:
        """The stage 1 exit condition: a deployment to Anvil the tool can read."""
        session = driver.run_to_settlement()

        result = Reconstructor(w3, deployment).run(session.session_id)
        _assert_all_passed(result, SETTLED_CHECKS)

        document = result.document
        assert document["outcome"]["kind"] == "settled"
        assert document["outcome"]["terminal_event"] == "SettlementCompleted"
        assert document["config_hash"]["recomputed"] == session.config_hash
        assert document["sequence_chain"]["sequences"] == [1, 2, 3]
        assert document["settlement"]["quote_amount"] == str(session.settled_quote_amount)
        assert document["settlement"]["base_amount"] == str(session.base_amount)

        # The amount the transfers were checked against came from the *signed offer*, not from the
        # settlement event. Recording both, and requiring the signed one to be present, is what
        # stops the tool certifying a settlement that moved an amount nobody authorised: comparing
        # transfers against the event would only prove the contract was self-consistent.
        settlement = document["settlement"]
        assert settlement["signed_quote_amount"] == str(session.settled_quote_amount)
        assert settlement["announced_quote_amount"] == settlement["signed_quote_amount"]
        assert settlement["quote_amount"] == settlement["signed_quote_amount"]

    def test_the_reconstruction_identifies_who_authorised_each_action(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver
    ) -> None:
        """The authority chain, recovered from calldata rather than read from an event.

        This is the part a demonstration rests on: the relay paid for all three transactions and
        authorised none of them.
        """
        session = driver.run_to_settlement()
        result = Reconstructor(w3, deployment).run(session.session_id)

        actions = result.document["actions"]
        assert [action["kind"] for action in actions] == ["offer", "offer", "accept"]
        assert actions[0]["recovered_signer"] == session.buyer
        assert actions[1]["recovered_signer"] == session.seller
        assert actions[2]["recovered_signer"] == session.buyer

        relay = Web3.to_checksum_address(deployment["relay_address"])
        assert all(action["submitted_by"] == relay for action in actions)
        assert all(action["recovered_signer"] != relay for action in actions)

    def test_the_accepted_offer_is_the_counter_not_the_opening_one(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver
    ) -> None:
        # A05 from the outside. The opening offer was displaced; the reconstruction must attribute
        # the settlement to the digest that was active, not to the first one it saw.
        session = driver.run_to_settlement()
        result = Reconstructor(w3, deployment).run(session.session_id)

        offers = [action for action in result.document["actions"] if action["kind"] == "offer"]
        accepted = result.document["actions"][-1]["typed_message"]["offerHash"]

        assert offers[0]["digest"] == session.offer_digests[0]
        assert offers[1]["digest"] == session.offer_digests[1]
        assert accepted == session.offer_digests[1]

    def test_a_walk_away_reconstructs_as_a_no_deal_that_moved_nothing(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver
    ) -> None:
        """A03. A no-deal is a result, and the evidence for it is that no tokens moved."""
        session = driver.run_to_walk_away()

        result = Reconstructor(w3, deployment).run(session.session_id)
        _assert_all_passed(result, UNSETTLED_CHECKS)

        outcome = result.document["outcome"]
        assert outcome["kind"] == "closed"
        assert outcome["reason_code"] == session.close_reason
        assert outcome["reason"] == "terms_unacceptable"
        assert "settlement" not in result.document

        deltas = result.document["balances"]["deltas"]
        assert deltas == {
            "buyer": {"base": "0", "quote": "0"},
            "seller": {"base": "0", "quote": "0"},
        }

    def test_an_operator_abort_reconstructs_with_its_reason(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver
    ) -> None:
        # An abort carries no participant signature and consumes no sequence, so the sequence chain
        # must still be contiguous over the one offer that preceded it (docs/protocol.md section 5).
        driver.fund(10_000_000, 250_000_000)
        config = driver.open_session(10_000_000)
        driver.record_offer(config, 1, driver.buyer, 90_000_000)
        driver.abort(config, 2)

        result = Reconstructor(w3, deployment).run(to_hex32(config.session_id))
        _assert_all_passed(result, UNSETTLED_CHECKS)

        outcome = result.document["outcome"]
        assert outcome["kind"] == "aborted"
        assert outcome["terminal_event"] == "SessionAborted"
        assert outcome["reason_code"] == 2
        assert outcome["reason"] == "model_failure"
        assert result.document["sequence_chain"]["sequences"] == [1]

    def test_an_unknown_session_id_does_not_reconstruct(
        self, w3: Web3, deployment: dict[str, Any]
    ) -> None:
        # The negative case that matters most. A tool that reported success for a session that was
        # never opened would make every other result meaningless.
        result = Reconstructor(w3, deployment).run("0x" + "ab" * 32)

        assert not result.ok
        assert any(check.check == "session_opened" and not check.ok for check in result.checks)

    def test_a_manifest_naming_the_wrong_exchange_does_not_reconstruct(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver
    ) -> None:
        """A17's automated half: identity is checked, not assumed.

        The manifest is pointed at the base token instead of the exchange. Nothing about the run
        changed, so a tool that trusted the manifest would report the same success as before.
        """
        session = driver.run_to_settlement()
        tampered = {**deployment, "exchange_address": deployment["base_token_address"]}

        result = Reconstructor(w3, tampered).run(session.session_id)

        assert not result.ok
        assert any(check.check == "exchange_code_hash" and not check.ok for check in result.checks)

    def test_a_manifest_with_the_wrong_code_hash_does_not_reconstruct(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver
    ) -> None:
        session = driver.run_to_settlement()
        tampered = {
            **deployment,
            "code_hashes": {**deployment["code_hashes"], "exchange": "0x" + "11" * 32},
        }

        result = Reconstructor(w3, tampered).run(session.session_id)

        assert not result.ok
        assert any(check.check == "exchange_code_hash" and not check.ok for check in result.checks)

    def test_a_manifest_naming_the_wrong_token_pair_fails_the_config_hash(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver
    ) -> None:
        """The `configHash` recomputation binds the token pair (docs/protocol.md section 3).

        Swapping base and quote in the manifest changes the recomputed hash, so the session on
        chain is no longer the session the manifest describes — which is the mis-wiring Q17's
        constructor guard prevents at deployment time and this catches after the fact.
        """
        session = driver.run_to_settlement()
        swapped = {
            **deployment,
            "base_token_address": deployment["quote_token_address"],
            "quote_token_address": deployment["base_token_address"],
        }

        result = Reconstructor(w3, swapped).run(session.session_id)

        assert not result.ok
        assert any(check.check == "config_hash" and not check.ok for check in result.checks)

    def test_the_reconstruction_document_is_serialisable(
        self, w3: Web3, deployment: dict[str, Any], driver: NegotiationDriver, tmp_path: Path
    ) -> None:
        # The document is the artefact the evaluator's invariant checker reads in stage 6, so it
        # has to survive a round trip through JSON with no bytes left in it.
        session = driver.run_to_settlement()
        result = Reconstructor(w3, deployment).run(session.session_id)

        target = tmp_path / "reconstruction.json"
        target.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
        reloaded = json.loads(target.read_text(encoding="utf-8"))

        assert reloaded["ok"] is True
        assert reloaded["session_id"] == session.session_id
        assert reloaded["outcome"]["kind"] == "settled"


@pytest.mark.integration
class TestDeployScriptPreconditions:
    """The deploy script's refusals, which run before any transaction is broadcast.

    A deployment that lands on chain and cannot be recorded, or that overwrites the record of an
    earlier one, is worse than one that never started — on Sepolia that record is the committed
    evidence a reader is asked to check. So each refusal is exercised here rather than trusted.
    """

    def test_the_script_refuses_to_overwrite_an_existing_manifest(
        self, anvil_rpc: str, deployment: dict[str, Any]
    ) -> None:
        # The `deployment` fixture has already written `local-test-run.json`, so this is the
        # second run with the same id.
        completed = run_deploy_script(
            anvil_rpc, deployment_id="local-test-run", manifest_dir=TEST_MANIFEST_DIR
        )

        assert completed.returncode != 0
        assert "a manifest already exists" in completed.stdout + completed.stderr

    def test_the_refusal_is_overridable_on_purpose(
        self, anvil_rpc: str, deployment: dict[str, Any], tmp_path: Path
    ) -> None:
        # A redeploy under a reused local id is a real thing to want; it just has to be said out
        # loud. The manifest is written to a scratch id so the session fixture's file is untouched.
        first = run_deploy_script(
            anvil_rpc, deployment_id="local-overwrite-me", manifest_dir=TEST_MANIFEST_DIR
        )
        assert first.returncode == 0, first.stdout + first.stderr

        again = run_deploy_script(
            anvil_rpc,
            deployment_id="local-overwrite-me",
            manifest_dir=TEST_MANIFEST_DIR,
            overwrite=True,
        )
        assert again.returncode == 0, again.stdout + again.stderr
        (TEST_MANIFEST_DIR / "local-overwrite-me.json").unlink(missing_ok=True)

    @pytest.mark.parametrize(
        ("deployment_id", "why"),
        [
            ("Local-Bad", "uppercase"),
            ("local--bad", "doubled hyphen"),
            ("-local-bad", "leading hyphen"),
            ("local-bad-", "trailing hyphen"),
            ("local_bad", "underscore"),
            ("local bad", "space"),
            ("", "empty"),
        ],
    )
    def test_an_id_the_manifest_schema_would_reject_is_refused_before_broadcast(
        self, anvil_rpc: str, deployment_id: str, why: str
    ) -> None:
        """Checked in the script, not left to the schema, because the schema is checked afterwards.

        Without this the script would broadcast three contracts and then write a manifest that
        fails its own `deployment_id` pattern — a deployment that exists and cannot be recorded.
        `--broadcast` is off here: the point is that the refusal happens before any of it.
        """
        completed = run_deploy_script(
            anvil_rpc,
            deployment_id=deployment_id,
            manifest_dir=TEST_MANIFEST_DIR,
            broadcast=False,
        )
        output = completed.stdout + completed.stderr

        assert completed.returncode != 0, why
        assert "DEPLOYMENT_ID" in output, why
        assert not (TEST_MANIFEST_DIR / f"{deployment_id}.json").exists(), why
