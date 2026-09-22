// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {BaseTest} from "../BaseTest.sol";
import {NegotiationExchange} from "../../src/NegotiationExchange.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice Who may sign, who may act, and who may only pay gas.
/// @dev Acceptance A08. docs/protocol.md section 1 and docs/security_and_trust_boundaries.md
///      section 4: the relay funds transactions and can never produce trading authority.
contract RolesTest is BaseTest {
    INegotiationExchange.SessionConfig internal config;

    function setUp() public override {
        super.setUp();
        config = openSession();
    }

    /// @dev A perfectly formed signature from a key that is not a participant. The message
    ///      names the stranger, so recovery succeeds and the participant check is what refuses.
    function test_A08_a_non_participant_signature_is_rejected() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, stranger, 94 * ONE_TOKEN);
        bytes memory signature = signOffer(STRANGER_PK, offer);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotParticipant.selector, stranger)
        );
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    /// @dev The message claims the buyer; the signature is the seller's. Recovery disagrees
    ///      with the claim, so neither party's authority is borrowed.
    function test_A08_a_signature_from_the_wrong_participant_is_rejected() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        bytes memory wrongKey = signOffer(SELLER_PK, offer);

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, wrongKey);
    }

    /// @dev The property the whole authority model rests on: the relay pays for every
    ///      transaction and cannot make one of its own.
    function test_A08_the_relay_cannot_manufacture_an_offer() public {
        uint256 relayPk = 0xEE1A4;
        address relayAddress = vm.addr(relayPk);

        INegotiationExchange.Offer memory offer =
            buildOffer(config, 1, relayAddress, 94 * ONE_TOKEN);
        bytes memory signature = signOffer(relayPk, offer);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotParticipant.selector, relayAddress)
        );
        vm.prank(relayAddress);
        exchange.recordOffer(offer, signature);
    }

    function test_A08_the_operator_cannot_sign_a_participant_action() public {
        uint256 operatorPk = 0x0BE4A;
        address operatorSigner = vm.addr(operatorPk);

        INegotiationExchange.Offer memory offer =
            buildOffer(config, 1, operatorSigner, 94 * ONE_TOKEN);
        bytes memory signature = signOffer(operatorPk, offer);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotParticipant.selector, operatorSigner)
        );
        vm.prank(operatorSigner);
        exchange.recordOffer(offer, signature);
    }

    // ---------------------------------------------------------------------------------
    // Signature authority on the entry points that move tokens and end sessions.
    //
    // These exist because an adversarial review mutated the contract and found the suite
    // did not notice: deleting `_requireSigner` from `acceptAndSettle`, and separately from
    // `closeSession`, left all 80 tests green. Every A07 and A08 case above targets
    // `recordOffer`, and `test_A08_self_acceptance_reverts` signs with the matching key, so
    // it exercises the actor-field check and survives removal of signature verification.
    //
    // docs/test_strategy.md section 4.1 names "wrong config hash; wrong domain;
    // non-participant" under `acceptAndSettle` specifically. `acceptAndSettle` is the only
    // function that moves tokens; an unverified signature there is an unauthorised transfer.
    // ---------------------------------------------------------------------------------

    function test_A08_settlement_rejects_a_signature_from_the_wrong_participant() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        // The message names the seller, who is the legitimate acceptor here; the signature
        // is the buyer's. Recovery disagrees with the claim.
        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory wrongKey = signAccept(BUYER_PK, acceptance);

        uint256 buyerBase = baseToken.balanceOf(buyer);

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, wrongKey);

        assertEq(baseToken.balanceOf(buyer), buyerBase, "no transfer on a bad signature");
    }

    function test_A08_settlement_rejects_a_non_participant_signature() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, stranger, offerHash);
        bytes memory signature = signAccept(STRANGER_PK, acceptance);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotParticipant.selector, stranger)
        );
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, signature);
    }

    /// @dev An empty signature is what an attacker submits when they have none. It must be
    ///      refused on the signature check, not merely by some later field comparison.
    function test_A08_settlement_rejects_an_empty_signature() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);

        uint256 sellerQuote = quoteToken.balanceOf(seller);

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(stranger);
        exchange.acceptAndSettle(acceptance, "");

        assertEq(quoteToken.balanceOf(seller), sellerQuote, "no transfer without authority");
    }

    /// @dev The relay pays gas for every settlement. It must not be able to author one.
    function test_A08_the_relay_cannot_manufacture_a_settlement() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        uint256 relayPk = 0xEE1A4;
        address relayAddress = vm.addr(relayPk);

        INegotiationExchange.Accept memory acceptance =
            buildAccept(config, 2, relayAddress, offerHash);
        bytes memory signature = signAccept(relayPk, acceptance);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotParticipant.selector, relayAddress)
        );
        vm.prank(relayAddress);
        exchange.acceptAndSettle(acceptance, signature);
    }

    /// @dev A07 at the settlement entry point: a signature made against another deployment's
    ///      domain does not settle here.
    function test_A07_settlement_rejects_a_signature_for_another_deployment() public {
        (, bytes32 offerHash) = recordOfferAs(config, 1, buyer, BUYER_PK, 96 * ONE_TOKEN);

        vm.prank(operator);
        NegotiationExchange other =
            new NegotiationExchange(address(baseToken), address(quoteToken), operator);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory foreignSignature = sign(SELLER_PK, other.hashAccept(acceptance));

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, foreignSignature);
    }

    function test_A08_close_rejects_a_signature_from_the_wrong_participant() public {
        INegotiationExchange.Close memory closure = buildClose(config, 1, buyer, 1);
        bytes memory wrongKey = signClose(SELLER_PK, closure);

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.closeSession(closure, wrongKey);

        assertEq(
            uint8(exchange.getSession(sessionId).status),
            uint8(INegotiationExchange.Status.Open),
            "session survives an unauthorised close"
        );
    }

    function test_A08_close_rejects_a_non_participant_signature() public {
        INegotiationExchange.Close memory closure = buildClose(config, 1, stranger, 1);
        bytes memory signature = signClose(STRANGER_PK, closure);

        vm.expectRevert(
            abi.encodeWithSelector(INegotiationExchange.NotParticipant.selector, stranger)
        );
        vm.prank(relay);
        exchange.closeSession(closure, signature);
    }

    /// @dev Without this, anyone could force a session Closed and destroy the distinction
    ///      between a walk-away and a settlement, which is the experiment's whole measurement.
    function test_A08_close_rejects_an_empty_signature() public {
        INegotiationExchange.Close memory closure = buildClose(config, 1, buyer, 3);

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(stranger);
        exchange.closeSession(closure, "");

        assertEq(
            uint8(exchange.getSession(sessionId).status), uint8(INegotiationExchange.Status.Open)
        );
    }

    function test_a_stranger_cannot_abort() public {
        vm.expectRevert(INegotiationExchange.NotOperator.selector);
        vm.prank(stranger);
        exchange.abortSession(sessionId, 1);
    }

    /// @dev Neither participant is the operator. Abort is an operator power and stays one.
    function test_a_participant_cannot_abort() public {
        vm.expectRevert(INegotiationExchange.NotOperator.selector);
        vm.prank(buyer);
        exchange.abortSession(sessionId, 1);
    }

    function test_immutables_are_exposed_for_the_setup_validator() public view {
        assertEq(exchange.baseToken(), address(baseToken));
        assertEq(exchange.quoteToken(), address(quoteToken));
        assertEq(exchange.operator(), operator);
    }
}
