// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {BaseTest} from "../BaseTest.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice Turn order, sequence, amount and lifetime rules for `recordOffer`.
/// @dev docs/protocol.md sections 5, 6 and 8.2; docs/test_strategy.md section 4.1.
contract RecordOfferTest is BaseTest {
    INegotiationExchange.SessionConfig internal config;

    function setUp() public override {
        super.setUp();
        config = openSession();
    }

    function test_buyer_offers_first_and_the_event_carries_the_digest() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        bytes32 offerHash = exchange.hashOffer(offer);

        vm.expectEmit(true, true, true, true, address(exchange));
        emit INegotiationExchange.OfferRecorded(
            sessionId, 1, buyer, 94 * ONE_TOKEN, offer.validUntil, offerHash
        );

        vm.prank(relay);
        exchange.recordOffer(offer, signOffer(BUYER_PK, offer));

        INegotiationExchange.SessionState memory state = exchange.getSession(sessionId);
        assertEq(state.sequence, 1, "sequence consumed");
        assertEq(state.offerCount, 1, "offerCount incremented");
        assertEq(state.activeOfferHash, offerHash, "active digest stored");
        assertEq(state.activeProposer, buyer, "active proposer stored");
        assertEq(state.activeQuoteAmount, 94 * ONE_TOKEN, "active amount stored");
        assertEq(state.activeValidUntil, offer.validUntil, "active validUntil stored");
        assertEq(state.activeSequence, 1, "active sequence stored");
    }

    /// @dev Rule 5.3. The seller cannot open, even with a valid signature and correct sequence.
    function test_seller_cannot_make_the_first_offer() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, seller, 105 * ONE_TOKEN);
        bytes memory signature = signOffer(SELLER_PK, offer);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotProposerTurn.selector, buyer)
        );
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    function test_turn_alternates_to_the_counterparty_of_the_last_proposer() public {
        recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);

        // The buyer cannot offer twice in a row.
        INegotiationExchange.Offer memory again = buildOffer(config, 2, buyer, 95 * ONE_TOKEN);
        bytes memory againSignature = signOffer(BUYER_PK, again);
        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotProposerTurn.selector, seller)
        );
        vm.prank(relay);
        exchange.recordOffer(again, againSignature);

        // The seller can, and then the turn returns to the buyer.
        recordOfferAs(config, 2, seller, SELLER_PK, 104 * ONE_TOKEN);
        assertEq(exchange.getSession(sessionId).activeProposer, seller);

        recordOfferAs(config, 3, buyer, BUYER_PK, 96 * ONE_TOKEN);
        assertEq(exchange.getSession(sessionId).activeProposer, buyer);
    }

    /// @dev Rule 5.3 says the turn rule holds "even when that offer has expired". An expired
    ///      offer is still the most recent recorded one, so the turn does not revert to the
    ///      proposer who made it.
    function test_turn_rule_survives_expiry_of_the_active_offer() public {
        recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);

        vm.warp(vm.getBlockTimestamp() + OFFER_LIFETIME + 1);

        INegotiationExchange.Offer memory buyerAgain = buildOffer(config, 2, buyer, 95 * ONE_TOKEN);
        bytes memory buyerAgainSignature = signOffer(BUYER_PK, buyerAgain);
        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotProposerTurn.selector, seller)
        );
        vm.prank(relay);
        exchange.recordOffer(buyerAgain, buyerAgainSignature);

        // The seller's turn still stands.
        recordOfferAs(config, 2, seller, SELLER_PK, 104 * ONE_TOKEN);
        assertEq(exchange.getSession(sessionId).offerCount, 2);
    }

    function test_replacing_an_offer_overwrites_the_active_digest() public {
        (, bytes32 firstHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);
        (, bytes32 secondHash) = recordOfferAs(config, 2, seller, SELLER_PK, 104 * ONE_TOKEN);

        assertTrue(firstHash != secondHash, "digests differ");
        assertEq(exchange.getSession(sessionId).activeOfferHash, secondHash);
    }

    function test_sequence_must_be_exactly_one_more_than_stored() public {
        INegotiationExchange.Offer memory skipped = buildOffer(config, 2, buyer, 94 * ONE_TOKEN);
        bytes memory skippedSignature = signOffer(BUYER_PK, skipped);
        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.SequenceMismatch.selector, 1, 2)
        );
        vm.prank(relay);
        exchange.recordOffer(skipped, skippedSignature);

        INegotiationExchange.Offer memory zero = buildOffer(config, 0, buyer, 94 * ONE_TOKEN);
        bytes memory zeroSignature = signOffer(BUYER_PK, zero);
        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.SequenceMismatch.selector, 1, 0)
        );
        vm.prank(relay);
        exchange.recordOffer(zero, zeroSignature);
    }

    function test_zero_quote_amount_reverts() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 0);
        bytes memory signature = signOffer(BUYER_PK, offer);
        vm.expectRevert(INegotiationExchange.InvalidQuoteAmount.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    function test_validUntil_at_or_before_now_reverts() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        offer.validUntil = uint64(block.timestamp);
        bytes memory signature = signOffer(BUYER_PK, offer);
        vm.expectRevert(INegotiationExchange.InvalidValidUntil.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    /// @dev An offer may not outlive the session it belongs to.
    function test_validUntil_after_session_expiry_reverts() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        offer.validUntil = config.expiresAt + 1;
        bytes memory signature = signOffer(BUYER_PK, offer);
        vm.expectRevert(INegotiationExchange.InvalidValidUntil.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    function test_validUntil_may_equal_session_expiry() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        offer.validUntil = config.expiresAt;
        vm.prank(relay);
        exchange.recordOffer(offer, signOffer(BUYER_PK, offer));
        assertEq(exchange.getSession(sessionId).activeValidUntil, config.expiresAt);
    }

    /// @dev The relay pays gas and holds no authority. Any submitter produces the same result,
    ///      which is what makes the record independent of who relayed it.
    function test_any_submitter_produces_the_identical_result() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        bytes memory signature = signOffer(BUYER_PK, offer);

        vm.prank(stranger);
        exchange.recordOffer(offer, signature);

        assertEq(exchange.getSession(sessionId).activeProposer, buyer);
        assertEq(exchange.getSession(sessionId).activeQuoteAmount, 94 * ONE_TOKEN);
    }

    function test_hashOffer_is_stable_and_field_sensitive() public view {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        bytes32 original = exchange.hashOffer(offer);
        assertEq(exchange.hashOffer(offer), original, "deterministic");

        offer.quoteAmount += 1;
        assertTrue(exchange.hashOffer(offer) != original, "amount is bound into the digest");
    }
}
