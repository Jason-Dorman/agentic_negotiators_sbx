// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {IERC20Errors} from "@openzeppelin/contracts/interfaces/draft-IERC6093.sol";
import {Vm} from "forge-std/Vm.sol";

import {BaseTest} from "../BaseTest.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice `acceptAndSettle`: the happy path with exact deltas, and every way it must refuse.
/// @dev Acceptance A05 (counteroffer then accept of the old offer), A08 (self-acceptance and
///      wrong participant), A10 (atomicity). docs/protocol.md section 8.2.
contract AcceptTest is BaseTest {
    INegotiationExchange.SessionConfig internal config;

    function setUp() public override {
        super.setUp();
        config = openSession();
    }

    /// @dev The whole point of the system in one test: both legs move, in one transaction, by
    ///      exactly the negotiated amounts, and nothing reaches the submitter.
    function test_settlement_moves_both_legs_by_exactly_the_agreed_amounts() public {
        uint256 quoteAmount = 96 * ONE_TOKEN;
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, quoteAmount);

        uint256 buyerBaseBefore = baseToken.balanceOf(buyer);
        uint256 buyerQuoteBefore = quoteToken.balanceOf(buyer);
        uint256 sellerBaseBefore = baseToken.balanceOf(seller);
        uint256 sellerQuoteBefore = quoteToken.balanceOf(seller);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.expectEmit(true, true, true, true, address(exchange));
        emit INegotiationExchange.AcceptanceRecorded(sessionId, 2, seller, offerHash);
        vm.expectEmit(true, true, true, true, address(exchange));
        emit INegotiationExchange.SettlementCompleted(
            sessionId,
            buyer,
            seller,
            address(baseToken),
            BASE_AMOUNT,
            address(quoteToken),
            quoteAmount,
            offerHash
        );

        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);

        assertEq(baseToken.balanceOf(buyer) - buyerBaseBefore, BASE_AMOUNT, "buyer receives base");
        assertEq(buyerQuoteBefore - quoteToken.balanceOf(buyer), quoteAmount, "buyer pays quote");
        assertEq(
            sellerBaseBefore - baseToken.balanceOf(seller), BASE_AMOUNT, "seller delivers base"
        );
        assertEq(
            quoteToken.balanceOf(seller) - sellerQuoteBefore, quoteAmount, "seller receives quote"
        );

        assertEq(baseToken.balanceOf(relay), 0, "submitter receives nothing");
        assertEq(quoteToken.balanceOf(relay), 0, "submitter receives nothing");
        assertEq(baseToken.balanceOf(address(exchange)), 0, "exchange holds nothing");
        assertEq(quoteToken.balanceOf(address(exchange)), 0, "exchange holds nothing");

        INegotiationExchange.SessionState memory state = exchange.getSession(sessionId);
        assertEq(uint8(state.status), uint8(INegotiationExchange.Status.Settled), "settled");
        assertEq(state.sequence, 2, "acceptance consumed a sequence");
        assertEq(state.activeOfferHash, bytes32(0), "active offer cleared");
    }

    /// @dev A05. The replaced digest is permanently unexecutable: a counterparty that signed an
    ///      acceptance of the earlier offer cannot land it after a replacement, which is what
    ///      stops a race from settling at a price nobody currently offers.
    function test_A05_accepting_a_replaced_offer_reverts_as_stale() public {
        (, bytes32 firstHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);
        (, bytes32 secondHash) = recordOfferAs(config, 2, seller, SELLER_PK, 104 * ONE_TOKEN);

        INegotiationExchange.Accept memory stale = buildAccept(config, 3, seller, firstHash);
        bytes memory signature = signAccept(SELLER_PK, stale);

        vm.expectRevert(
            abi.encodeWithSelector(
                INegotiationExchange.StaleOfferDigest.selector, secondHash, firstHash
            )
        );
        vm.prank(relay);
        exchange.acceptAndSettle(stale, signature);

        assertEq(
            uint8(exchange.getSession(sessionId).status),
            uint8(INegotiationExchange.Status.Open),
            "session untouched"
        );
    }

    /// @dev A08. The proposer cannot take their own offer: an offer authorises the other side.
    function test_A08_self_acceptance_reverts() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, buyer, offerHash);
        bytes memory signature = signAccept(BUYER_PK, acceptance);

        vm.expectRevert(INegotiationExchange.SelfAcceptance.selector);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);
    }

    function test_accepting_with_no_active_offer_reverts() public {
        INegotiationExchange.Accept memory acceptance =
            buildAccept(config, 1, seller, keccak256("nothing"));
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.expectRevert(INegotiationExchange.NoActiveOffer.selector);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);
    }

    function test_accepting_after_the_offer_lifetime_reverts() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.warp(block.timestamp + OFFER_LIFETIME);

        vm.expectRevert(INegotiationExchange.OfferExpired.selector);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);
    }

    function test_acceptance_one_second_before_the_lifetime_ends_succeeds() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.warp(block.timestamp + OFFER_LIFETIME - 1);

        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);
        assertEq(
            uint8(exchange.getSession(sessionId).status), uint8(INegotiationExchange.Status.Settled)
        );
    }

    function test_wrong_sequence_reverts() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 94 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 3, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.SequenceMismatch.selector, 2, 3)
        );
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);
    }

    /// @dev A10. Effects land before the transfers, so a failing second leg leaves no settled
    ///      session behind: both balances and the status are exactly as before.
    function test_A10_a_failing_second_leg_reverts_the_whole_settlement() public {
        uint256 quoteAmount = 96 * ONE_TOKEN;
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, quoteAmount);

        // The seller revokes the base-token allowance after offering authority was granted.
        vm.prank(seller);
        baseToken.approve(address(exchange), 0);

        uint256 buyerQuoteBefore = quoteToken.balanceOf(buyer);
        uint256 sellerQuoteBefore = quoteToken.balanceOf(seller);
        uint256 sellerBaseBefore = baseToken.balanceOf(seller);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.expectRevert(
            abi.encodeWithSelector(
                IERC20Errors.ERC20InsufficientAllowance.selector, address(exchange), 0, BASE_AMOUNT
            )
        );
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);

        assertEq(quoteToken.balanceOf(buyer), buyerQuoteBefore, "first leg rolled back");
        assertEq(quoteToken.balanceOf(seller), sellerQuoteBefore, "first leg rolled back");
        assertEq(baseToken.balanceOf(seller), sellerBaseBefore, "second leg never happened");

        INegotiationExchange.SessionState memory state = exchange.getSession(sessionId);
        assertEq(uint8(state.status), uint8(INegotiationExchange.Status.Open), "still Open");
        assertEq(state.activeOfferHash, offerHash, "offer still active");
        assertEq(state.sequence, 1, "sequence not consumed");
    }

    /// @dev The same guarantee from the other side: the buyer cannot pay, so nothing moves.
    function test_A10_an_insufficient_buyer_balance_reverts_the_whole_settlement() public {
        uint256 quoteAmount = 96 * ONE_TOKEN;
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, quoteAmount);

        // Read the balance before the prank: `balanceOf` is an external call and would
        // otherwise consume it, leaving the transfer to run as the test contract.
        uint256 buyerQuote = quoteToken.balanceOf(buyer);
        vm.prank(buyer);
        quoteToken.transfer(stranger, buyerQuote);

        uint256 sellerBaseBefore = baseToken.balanceOf(seller);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.expectRevert(
            abi.encodeWithSelector(
                IERC20Errors.ERC20InsufficientBalance.selector, buyer, 0, quoteAmount
            )
        );
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);

        assertEq(baseToken.balanceOf(seller), sellerBaseBefore, "base never moved");
        assertEq(
            uint8(exchange.getSession(sessionId).status),
            uint8(INegotiationExchange.Status.Open),
            "still Open"
        );
    }

    /// @dev docs/protocol.md section 8.2 fixes the order: quote from buyer to seller, then
    ///      base from seller to buyer. The order is normative, so it is pinned here rather
    ///      than left to the two "a failing leg reverts everything" tests above — those pass
    ///      with the legs swapped, because whichever leg fails, everything reverts either way.
    ///      An adversarial review swapped the two calls in the contract and the whole suite
    ///      stayed green; this test is what now fails when that happens.
    function test_settlement_moves_the_quote_leg_before_the_base_leg() public {
        uint256 quoteAmount = 96 * ONE_TOKEN;
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, quoteAmount);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.recordLogs();
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bytes32 transferTopic = keccak256("Transfer(address,address,uint256)");
        int256 quoteIndex = -1;
        int256 baseIndex = -1;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] != transferTopic) continue;
            if (logs[i].emitter == address(quoteToken) && quoteIndex < 0) quoteIndex = int256(i);
            if (logs[i].emitter == address(baseToken) && baseIndex < 0) baseIndex = int256(i);
        }

        assertGe(quoteIndex, 0, "quote leg emitted a Transfer");
        assertGe(baseIndex, 0, "base leg emitted a Transfer");
        assertLt(quoteIndex, baseIndex, "quote leg settles before the base leg (protocol 8.2)");
    }

    /// @dev The settlement clearing writes and `activeSequence` were asserted nowhere, so a
    ///      contract that left the active offer populated after settling would pass. Status is
    ///      terminal so nothing could act on it, but the export and the reconstruction read
    ///      this state and a stale active offer would misreport what was agreed.
    function test_settlement_clears_every_active_offer_field() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        INegotiationExchange.SessionState memory before = exchange.getSession(sessionId);
        assertEq(before.activeSequence, 1, "the recorded offer's sequence is stored");
        assertEq(before.activeValidUntil > 0, true, "validUntil is stored");

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signAccept(SELLER_PK, acceptance));

        INegotiationExchange.SessionState memory state = exchange.getSession(sessionId);
        assertEq(state.activeOfferHash, bytes32(0), "digest cleared");
        assertEq(state.activeProposer, address(0), "proposer cleared");
        assertEq(state.activeQuoteAmount, 0, "amount cleared");
        assertEq(state.activeValidUntil, 0, "validUntil cleared");
        assertEq(state.activeSequence, 0, "activeSequence cleared");
        assertEq(state.offerCount, 1, "offerCount is history and is not cleared");
        assertEq(state.sequence, 2, "the acceptance's sequence is the last consumed");
    }

    /// @dev A third-party submitter produces the identical result, so the record does not
    ///      depend on the relay being the relay.
    function test_a_stranger_may_submit_the_settlement() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory signature = signAccept(SELLER_PK, acceptance);

        vm.prank(stranger);
        exchange.acceptAndSettle(acceptance, signature);

        assertEq(baseToken.balanceOf(buyer), BASE_AMOUNT);
        assertEq(baseToken.balanceOf(stranger), 0, "submitter gains nothing");
    }
}
