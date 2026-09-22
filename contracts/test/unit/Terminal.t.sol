// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {BaseTest} from "../BaseTest.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice Close and abort, and the rule that terminal means terminal.
/// @dev Acceptance A11. The four terminal statuses are separate on purpose: walk-away, expiry,
///      operator abort and settlement are different results and the evaluation has to be able
///      to tell them apart (docs/architecture.md section 1, docs/prd.md).
contract TerminalTest is BaseTest {
    INegotiationExchange.SessionConfig internal config;

    function setUp() public override {
        super.setUp();
        config = openSession();
    }

    /// @dev Rule 5.4: either participant may close whenever the session is Open, in or out of
    ///      turn. A party that will not trade need not wait for its turn to say so.
    function test_either_participant_may_close_out_of_turn() public {
        recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);

        // It is the seller's turn to offer, and the buyer closes instead.
        INegotiationExchange.Close memory closure = buildClose(config, 2, buyer, 1);

        vm.expectEmit(true, true, true, true, address(exchange));
        emit INegotiationExchange.SessionClosed(sessionId, 2, buyer, 1);

        vm.prank(relay);
        exchange.closeSession(closure, signClose(BUYER_PK, closure));

        INegotiationExchange.SessionState memory state = exchange.getSession(sessionId);
        assertEq(uint8(state.status), uint8(INegotiationExchange.Status.Closed));
        assertEq(state.sequence, 2, "close consumes a sequence");
    }

    function test_every_defined_close_reason_is_accepted() public {
        for (uint8 reason = 1; reason <= 3; reason++) {
            INegotiationExchange.SessionConfig memory fresh = defaultConfig();
            fresh.sessionId = keccak256(abi.encode("close-reason", reason));
            vm.prank(operator);
            exchange.createSession(fresh);

            INegotiationExchange.Close memory closure = buildClose(fresh, 1, seller, reason);
            vm.prank(relay);
            exchange.closeSession(closure, signClose(SELLER_PK, closure));

            assertEq(
                uint8(exchange.getSession(fresh.sessionId).status),
                uint8(INegotiationExchange.Status.Closed)
            );
        }
    }

    function test_undefined_close_reasons_revert() public {
        uint8[2] memory bad = [uint8(0), uint8(4)];
        for (uint256 i = 0; i < bad.length; i++) {
            INegotiationExchange.Close memory closure = buildClose(config, 1, buyer, bad[i]);
            bytes memory signature = signClose(BUYER_PK, closure);

            vm.expectRevert(
                abi.encodeWithSelector(INegotiationExchange.InvalidReason.selector, bad[i])
            );
            vm.prank(relay);
            exchange.closeSession(closure, signature);
        }
    }

    function test_every_defined_abort_reason_is_accepted() public {
        for (uint8 reason = 1; reason <= 4; reason++) {
            INegotiationExchange.SessionConfig memory fresh = defaultConfig();
            fresh.sessionId = keccak256(abi.encode("abort-reason", reason));
            vm.prank(operator);
            exchange.createSession(fresh);

            vm.expectEmit(true, true, true, true, address(exchange));
            emit INegotiationExchange.SessionAborted(fresh.sessionId, operator, reason);

            vm.prank(operator);
            exchange.abortSession(fresh.sessionId, reason);
        }
    }

    function test_undefined_abort_reasons_revert() public {
        uint8[2] memory bad = [uint8(0), uint8(5)];
        for (uint256 i = 0; i < bad.length; i++) {
            vm.expectRevert(
                abi.encodeWithSelector(INegotiationExchange.InvalidReason.selector, bad[i])
            );
            vm.prank(operator);
            exchange.abortSession(sessionId, bad[i]);
        }
    }

    /// @dev Abort does not consume a sequence: it carries no participant signature.
    function test_abort_leaves_the_sequence_untouched() public {
        recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);

        vm.prank(operator);
        exchange.abortSession(sessionId, 2);

        INegotiationExchange.SessionState memory state = exchange.getSession(sessionId);
        assertEq(uint8(state.status), uint8(INegotiationExchange.Status.Aborted));
        assertEq(state.sequence, 1, "abort is a lifecycle action, not a signed one");
    }

    /// @dev A11 in full: after each terminal status, every action is refused with the status
    ///      that already holds. An aborted negotiation cannot be settled by a late acceptance.
    function test_A11_no_action_succeeds_after_a_terminal_status() public {
        _assertAllActionsRefusedAfter(_closeIt, INegotiationExchange.Status.Closed);
        _assertAllActionsRefusedAfter(_abortIt, INegotiationExchange.Status.Aborted);
        _assertAllActionsRefusedAfter(_settleIt, INegotiationExchange.Status.Settled);
        // Expired last: reaching it warps past this session's deadline, and each helper
        // creates its session from the current timestamp.
        _assertAllActionsRefusedAfter(_expireIt, INegotiationExchange.Status.Expired);
    }

    /// @dev The abort-versus-accept race, resolved on chain rather than by timing luck: whoever
    ///      lands first wins, and the loser gets a status revert rather than a second outcome.
    function test_A11_a_late_acceptance_after_abort_is_refused() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.prank(operator);
        exchange.abortSession(sessionId, 1);

        uint256 buyerBase = baseToken.balanceOf(buyer);

        vm.expectRevert(
            abi.encodeWithSelector(
                INegotiationExchange.SessionNotOpen.selector,
                uint8(INegotiationExchange.Status.Aborted)
            )
        );
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);

        assertEq(baseToken.balanceOf(buyer), buyerBase, "no transfer after abort");
    }

    function test_actions_on_an_unknown_session_report_status_None() public {
        INegotiationExchange.SessionConfig memory unknown = defaultConfig();
        unknown.sessionId = keccak256("unknown");

        INegotiationExchange.Offer memory offer = buildOffer(unknown, 1, buyer, 94 * ONE_TOKEN);
        bytes memory signature = signOffer(BUYER_PK, offer);

        vm.expectRevert(
            abi.encodeWithSelector(
                INegotiationExchange.SessionNotOpen.selector,
                uint8(INegotiationExchange.Status.None)
            )
        );
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    // ---------------------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------------------

    function _assertAllActionsRefusedAfter(
        function(INegotiationExchange.SessionConfig memory) internal reachTerminal,
        INegotiationExchange.Status expected
    ) private {
        INegotiationExchange.SessionConfig memory fresh = defaultConfig();
        fresh.sessionId = keccak256(abi.encode("terminal", uint8(expected)));
        vm.prank(operator);
        exchange.createSession(fresh);

        reachTerminal(fresh);
        assertEq(uint8(exchange.getSession(fresh.sessionId).status), uint8(expected), "reached");

        bytes memory expectedRevert =
            abi.encodeWithSelector(INegotiationExchange.SessionNotOpen.selector, uint8(expected));

        INegotiationExchange.Offer memory offer = buildOffer(fresh, 9, buyer, 94 * ONE_TOKEN);
        bytes memory offerSignature = signOffer(BUYER_PK, offer);
        vm.expectRevert(expectedRevert);
        vm.prank(relay);
        exchange.recordOffer(offer, offerSignature);

        INegotiationExchange.Accept memory acceptance =
            buildAccept(fresh, 9, seller, keccak256("anything"));
        bytes memory acceptSignature = signAccept(SELLER_PK, acceptance);
        vm.expectRevert(expectedRevert);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, acceptSignature);

        INegotiationExchange.Close memory closure = buildClose(fresh, 9, buyer, 1);
        bytes memory closeSignature = signClose(BUYER_PK, closure);
        vm.expectRevert(expectedRevert);
        vm.prank(relay);
        exchange.closeSession(closure, closeSignature);

        vm.expectRevert(expectedRevert);
        vm.prank(operator);
        exchange.abortSession(fresh.sessionId, 1);

        vm.expectRevert(expectedRevert);
        vm.prank(stranger);
        exchange.expireSession(fresh.sessionId);
    }

    function _closeIt(INegotiationExchange.SessionConfig memory fresh) private {
        INegotiationExchange.Close memory closure = buildClose(fresh, 1, buyer, 1);
        vm.prank(relay);
        exchange.closeSession(closure, signClose(BUYER_PK, closure));
    }

    function _expireIt(INegotiationExchange.SessionConfig memory fresh) private {
        vm.warp(fresh.expiresAt);
        vm.prank(stranger);
        exchange.expireSession(fresh.sessionId);
    }

    function _abortIt(INegotiationExchange.SessionConfig memory fresh) private {
        vm.prank(operator);
        exchange.abortSession(fresh.sessionId, 1);
    }

    function _settleIt(INegotiationExchange.SessionConfig memory fresh) private {
        INegotiationExchange.Offer memory offer = buildOffer(fresh, 1, buyer, 96 * ONE_TOKEN);
        bytes32 offerHash = exchange.hashOffer(offer);
        vm.prank(relay);
        exchange.recordOffer(offer, signOffer(BUYER_PK, offer));

        INegotiationExchange.Accept memory acceptance = buildAccept(fresh, 2, seller, offerHash);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signAccept(SELLER_PK, acceptance));
    }
}
