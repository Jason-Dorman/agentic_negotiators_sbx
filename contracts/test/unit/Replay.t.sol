// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {BaseTest} from "../BaseTest.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice Submitting the same signed action twice, and settling twice.
/// @dev Acceptance A06. The relay rebroadcasts on its own; the contract has to make a duplicate
///      arrival harmless, which is what lets the application treat a second receipt as
///      "already complete" rather than as a second economic event
///      (docs/architecture.md section 5.4).
contract ReplayTest is BaseTest {
    INegotiationExchange.SessionConfig internal config;

    function setUp() public override {
        super.setUp();
        config = openSession();
    }

    /// @dev The sequence has moved on, so the identical bytes cannot land twice.
    function test_A06_replaying_a_recorded_offer_reverts_on_sequence() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        bytes memory signature = signOffer(BUYER_PK, offer);

        vm.prank(relay);
        exchange.recordOffer(offer, signature);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.SequenceMismatch.selector, 2, 1)
        );
        vm.prank(relay);
        exchange.recordOffer(offer, signature);

        assertEq(exchange.getSession(sessionId).offerCount, 1, "counted once");
    }

    /// @dev A second settlement of a settled session is refused on status, before anything
    ///      touches a token. One acceptance, one transfer pair, ever.
    function test_A06_a_second_settlement_reverts_on_status() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);

        uint256 buyerBase = baseToken.balanceOf(buyer);
        uint256 sellerQuote = quoteToken.balanceOf(seller);

        vm.expectRevert(
            abi.encodeWithSelector(
                INegotiationExchange.SessionNotOpen.selector,
                uint8(INegotiationExchange.Status.Settled)
            )
        );
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);

        assertEq(baseToken.balanceOf(buyer), buyerBase, "no second base transfer");
        assertEq(quoteToken.balanceOf(seller), sellerQuote, "no second quote transfer");
    }

    function test_A06_replaying_a_close_reverts_on_status() public {
        INegotiationExchange.Close memory closure = buildClose(config, 1, buyer, 1);
        bytes memory signature = signClose(BUYER_PK, closure);

        vm.prank(relay);
        exchange.closeSession(closure, signature);

        vm.expectRevert(
            abi.encodeWithSelector(
                INegotiationExchange.SessionNotOpen.selector,
                uint8(INegotiationExchange.Status.Closed)
            )
        );
        vm.prank(relay);
        exchange.closeSession(closure, signature);
    }

    /// @dev `expireSession` takes no signature, so its idempotency rests on status alone.
    function test_A06_expiring_twice_reverts_the_second_time() public {
        vm.warp(config.expiresAt);

        vm.prank(stranger);
        exchange.expireSession(sessionId);

        vm.expectRevert(
            abi.encodeWithSelector(
                INegotiationExchange.SessionNotOpen.selector,
                uint8(INegotiationExchange.Status.Expired)
            )
        );
        vm.prank(stranger);
        exchange.expireSession(sessionId);
    }
}
