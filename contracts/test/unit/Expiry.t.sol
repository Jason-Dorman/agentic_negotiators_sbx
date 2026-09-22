// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {BaseTest} from "../BaseTest.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice Session deadline behaviour: what the clock permits and what it forecloses.
/// @dev Acceptance A09. docs/protocol.md section 6. After `expiresAt` only `expireSession`
///      succeeds, so a stalled negotiation resolves to one recorded outcome and not to silence.
contract ExpiryTest is BaseTest {
    INegotiationExchange.SessionConfig internal config;

    function setUp() public override {
        super.setUp();
        config = openSession();
    }

    function test_expire_before_the_deadline_reverts() public {
        vm.warp(config.expiresAt - 1);
        vm.expectRevert(INegotiationExchange.SessionNotExpired.selector);
        vm.prank(stranger);
        exchange.expireSession(sessionId);
    }

    /// @dev Anyone may expire. It is a public fact about the chain clock, not a privilege.
    function test_A09_anyone_may_expire_at_the_deadline() public {
        vm.warp(config.expiresAt);

        vm.expectEmit(true, true, true, true, address(exchange));
        emit INegotiationExchange.SessionExpired(sessionId, config.expiresAt);

        vm.prank(stranger);
        exchange.expireSession(sessionId);

        assertEq(
            uint8(exchange.getSession(sessionId).status), uint8(INegotiationExchange.Status.Expired)
        );
    }

    /// @dev The stored status is still Open, but the clock has passed: a participant action
    ///      must fail on the deadline rather than on the status.
    function test_A09_participant_actions_fail_on_the_deadline_even_while_status_is_open() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        offer.validUntil = config.expiresAt;
        bytes memory signature = signOffer(BUYER_PK, offer);

        vm.warp(config.expiresAt);
        assertEq(
            uint8(exchange.getSession(sessionId).status),
            uint8(INegotiationExchange.Status.Open),
            "status is still Open"
        );

        vm.expectRevert(INegotiationExchange.SessionDeadlinePassed.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    function test_A09_close_after_the_deadline_reverts() public {
        INegotiationExchange.Close memory closure = buildClose(config, 1, buyer, 1);
        bytes memory signature = signClose(BUYER_PK, closure);

        vm.warp(config.expiresAt);

        vm.expectRevert(INegotiationExchange.SessionDeadlinePassed.selector);
        vm.prank(relay);
        exchange.closeSession(closure, signature);
    }

    /// @dev An abort after the deadline would let the operator relabel an expiry. It reverts,
    ///      so the recorded outcome of a session that ran out of time is always Expired.
    function test_A09_abort_after_the_deadline_reverts() public {
        vm.warp(config.expiresAt);

        vm.expectRevert(INegotiationExchange.SessionDeadlinePassed.selector);
        vm.prank(operator);
        exchange.abortSession(sessionId, 1);
    }

    function test_A09_settlement_after_the_deadline_reverts() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);
        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.warp(config.expiresAt);

        vm.expectRevert(INegotiationExchange.SessionDeadlinePassed.selector);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);
    }

    function test_actions_one_second_before_the_deadline_still_succeed() public {
        vm.warp(config.expiresAt - 1);

        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        vm.prank(relay);
        exchange.recordOffer(offer, signOffer(BUYER_PK, offer));

        assertEq(exchange.getSession(sessionId).offerCount, 1);
    }

    function test_expiring_an_unknown_session_reverts_as_not_open() public {
        vm.expectRevert(
            abi.encodeWithSelector(
                INegotiationExchange.SessionNotOpen.selector,
                uint8(INegotiationExchange.Status.None)
            )
        );
        exchange.expireSession(keccak256("never created"));
    }
}
