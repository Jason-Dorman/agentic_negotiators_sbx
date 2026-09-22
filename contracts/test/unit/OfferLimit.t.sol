// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {BaseTest} from "../BaseTest.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice The offer budget, and what remains possible once it is spent.
/// @dev Acceptance A09. docs/protocol.md rule 5.7: `recordOffer` reverts at the limit, but
///      acceptance and close stay available, so a negotiation that runs out of offers still
///      reaches a recorded outcome rather than stalling.
contract OfferLimitTest is BaseTest {
    INegotiationExchange.SessionConfig internal config;

    function setUp() public override {
        super.setUp();
        config = openSession();
    }

    /// @dev With `maxOffers = 8`, the eighth offer is acceptable and the ninth reverts.
    function test_A09_the_eighth_offer_lands_and_the_ninth_reverts() public {
        _fillOfferBudget();

        assertEq(exchange.getSession(sessionId).offerCount, MAX_OFFERS, "budget spent");

        // The ninth. Sequence and turn are both correct; only the budget refuses.
        address ninthProposer = MAX_OFFERS % 2 == 0 ? buyer : seller;
        uint256 ninthKey = MAX_OFFERS % 2 == 0 ? BUYER_PK : SELLER_PK;
        INegotiationExchange.Offer memory ninth =
            buildOffer(config, MAX_OFFERS + 1, ninthProposer, 100 * ONE_TOKEN);
        bytes memory signature = signOffer(ninthKey, ninth);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.OfferLimitReached.selector, MAX_OFFERS)
        );
        vm.prank(relay);
        exchange.recordOffer(ninth, signature);
    }

    /// @dev The last offer is still live after the budget is spent, so it can be accepted.
    function test_A09_acceptance_remains_available_after_the_offer_budget_is_spent() public {
        bytes32 lastHash = _fillOfferBudget();

        INegotiationExchange.Accept memory acceptance =
            buildAccept(config, MAX_OFFERS + 1, buyer, lastHash);
        bytes memory signature = signAccept(BUYER_PK, acceptance);

        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);

        assertEq(
            uint8(exchange.getSession(sessionId).status), uint8(INegotiationExchange.Status.Settled)
        );
    }

    function test_A09_close_remains_available_after_the_offer_budget_is_spent() public {
        _fillOfferBudget();

        INegotiationExchange.Close memory closure = buildClose(config, MAX_OFFERS + 1, buyer, 3); // no_further_concession
        bytes memory signature = signClose(BUYER_PK, closure);

        vm.prank(relay);
        exchange.closeSession(closure, signature);

        assertEq(
            uint8(exchange.getSession(sessionId).status), uint8(INegotiationExchange.Status.Closed)
        );
    }

    /// @dev With a budget of one, the buyer's single offer exhausts it and the seller may only
    ///      accept or close.
    function test_a_single_offer_budget_leaves_the_seller_only_accept_or_close() public {
        INegotiationExchange.SessionConfig memory single = defaultConfig();
        single.sessionId = keccak256("single-offer");
        single.maxOffers = 1;
        vm.prank(operator);
        exchange.createSession(single);

        INegotiationExchange.Offer memory offer = buildOffer(single, 1, buyer, 96 * ONE_TOKEN);
        bytes32 offerHash = exchange.hashOffer(offer);
        vm.prank(relay);
        exchange.recordOffer(offer, signOffer(BUYER_PK, offer));

        INegotiationExchange.Offer memory counter = buildOffer(single, 2, seller, 104 * ONE_TOKEN);
        bytes memory counterSignature = signOffer(SELLER_PK, counter);
        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.OfferLimitReached.selector, uint16(1))
        );
        vm.prank(relay);
        exchange.recordOffer(counter, counterSignature);

        INegotiationExchange.Accept memory acceptance = buildAccept(single, 2, seller, offerHash);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signAccept(SELLER_PK, acceptance));

        assertEq(
            uint8(exchange.getSession(single.sessionId).status),
            uint8(INegotiationExchange.Status.Settled)
        );
    }

    /// @dev Record `MAX_OFFERS` alternating offers and return the last digest.
    function _fillOfferBudget() private returns (bytes32 lastHash) {
        for (uint16 i = 0; i < MAX_OFFERS; i++) {
            bool buyerTurn = i % 2 == 0;
            (, lastHash) = recordOfferAs(
                config,
                uint64(i) + 1,
                buyerTurn ? buyer : seller,
                buyerTurn ? BUYER_PK : SELLER_PK,
                (buyerTurn ? 94 : 104) * ONE_TOKEN + i
            );
        }
    }
}
