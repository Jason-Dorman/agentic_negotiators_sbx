// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {BaseTest} from "../BaseTest.sol";
import {NegotiationExchange} from "../../src/NegotiationExchange.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice Stateless fuzzing over the arithmetic and the boundaries the unit suite tests at
///         their edges only.
/// @dev docs/test_strategy.md section 4.2. The unit tests ask "does `maxOffers = 33` revert";
///      these ask "is there *any* value outside 1..32 that does not", which is the question a
///      reader of the protocol document actually has. The handler-based suite in this directory
///      covers sequences of actions; this file covers single actions over their whole domain.
contract FuzzTest is BaseTest {
    /// @notice `configHash` matches the section 3 encoding for arbitrary session terms.
    /// @dev Computed off-chain from the fields and compared with what the contract stored, so
    ///      the two encodings are checked against each other rather than against themselves.
    ///      This is the hash each agent service recomputes before it will sign anything, and a
    ///      one-field disagreement makes every signature in the run unusable.
    function testFuzz_configHash_matches_the_off_chain_encoding(
        bytes32 sessionId_,
        uint256 baseAmount,
        uint64 duration,
        uint16 maxOffers
    ) public {
        baseAmount = bound(baseAmount, 1, type(uint128).max);
        duration = uint64(bound(uint256(duration), 1, 365 days));
        maxOffers = uint16(bound(uint256(maxOffers), 1, 32));

        INegotiationExchange.SessionConfig memory config = INegotiationExchange.SessionConfig({
            sessionId: sessionId_,
            buyer: buyer,
            seller: seller,
            baseAmount: baseAmount,
            expiresAt: uint64(block.timestamp) + duration,
            maxOffers: maxOffers
        });

        vm.prank(operator);
        exchange.createSession(config);

        assertEq(exchange.getSession(sessionId_).configHash, expectedConfigHash(config));
    }

    /// @notice `maxOffers` is accepted exactly on 1..32 and refused everywhere else.
    function testFuzz_max_offers_is_accepted_only_within_its_range(uint16 maxOffers) public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.maxOffers = maxOffers;

        if (maxOffers >= 1 && maxOffers <= 32) {
            vm.prank(operator);
            exchange.createSession(config);
            assertEq(exchange.getSession(config.sessionId).config.maxOffers, maxOffers);
        } else {
            vm.prank(operator);
            vm.expectRevert(INegotiationExchange.InvalidMaxOffers.selector);
            exchange.createSession(config);
        }
    }

    /// @notice A settlement moves exactly the signed quote and the configured base, at any price.
    /// @dev The property the whole system rests on: the amount that moves is the amount that was
    ///      signed, with no rounding, no fee and no adjustment anywhere in the path.
    function testFuzz_settlement_moves_exactly_the_signed_amounts(
        uint256 quoteAmount,
        uint64 lifetime
    ) public {
        quoteAmount = bound(quoteAmount, 1, BUYER_QUOTE_BALANCE);
        lifetime = uint64(bound(uint256(lifetime), 1, uint256(OFFER_LIFETIME)));

        INegotiationExchange.SessionConfig memory config = openSession();

        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, quoteAmount);
        offer.validUntil = uint64(block.timestamp) + lifetime;
        bytes memory offerSignature = signOffer(BUYER_PK, offer);
        bytes32 offerHash = exchange.hashOffer(offer);

        vm.prank(relay);
        exchange.recordOffer(offer, offerSignature);

        INegotiationExchange.Accept memory acceptance = buildAccept(config, 2, seller, offerHash);
        bytes memory acceptSignature = signAccept(SELLER_PK, acceptance);

        vm.prank(relay);
        exchange.acceptAndSettle(acceptance, acceptSignature);

        assertEq(baseToken.balanceOf(buyer), BASE_AMOUNT, "buyer base credit");
        assertEq(baseToken.balanceOf(seller), 0, "seller base debit");
        assertEq(quoteToken.balanceOf(seller), quoteAmount, "seller quote credit");
        assertEq(
            quoteToken.balanceOf(buyer), BUYER_QUOTE_BALANCE - quoteAmount, "buyer quote debit"
        );
        assertEq(
            uint8(exchange.getSession(config.sessionId).status),
            uint8(INegotiationExchange.Status.Settled)
        );
    }

    /// @notice Only the sequence one past the stored one is accepted.
    /// @dev Replay protection is a range check, not an inequality: a sequence *ahead* of the
    ///      expected one is as invalid as one behind it, because a gap would let an action be
    ///      recorded out of order and the reconstruction tool's contiguity check (A15) would
    ///      then be checking a chain the contract never enforced.
    function testFuzz_only_the_next_sequence_is_accepted(uint64 sequence) public {
        INegotiationExchange.SessionConfig memory config = openSession();

        INegotiationExchange.Offer memory offer =
            buildOffer(config, sequence, buyer, 92 * ONE_TOKEN);
        bytes memory signature = signOffer(BUYER_PK, offer);

        if (sequence == 1) {
            vm.prank(relay);
            exchange.recordOffer(offer, signature);
            assertEq(exchange.getSession(config.sessionId).sequence, 1);
        } else {
            vm.prank(relay);
            vm.expectRevert(
                abi.encodeWithSelector(INegotiationExchange.SequenceMismatch.selector, 1, sequence)
            );
            exchange.recordOffer(offer, signature);
        }
    }

    /// @notice `validUntil` is accepted only strictly after now and no later than session expiry.
    /// @dev Bounded to a window that straddles both edges, rather than fuzzed over the whole
    ///      `uint64` range. Unbounded, a value landing inside `(now, expiresAt]` is about one in
    ///      10^15, so the legal branch — and its `activeValidUntil` assertion — was dead code at
    ///      any run count: the test only ever proved that absurd values are refused. Straddling the
    ///      bounds by ten seconds exercises both sides and keeps the boundaries themselves,
    ///      `now` and `expiresAt`, reachable.
    function testFuzz_valid_until_is_accepted_only_within_its_window(uint64 validUntil) public {
        INegotiationExchange.SessionConfig memory config = openSession();

        validUntil = uint64(
            bound(
                uint256(validUntil), uint256(block.timestamp) - 10, uint256(config.expiresAt) + 10
            )
        );

        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 92 * ONE_TOKEN);
        offer.validUntil = validUntil;
        bytes memory signature = signOffer(BUYER_PK, offer);

        bool legal = validUntil > block.timestamp && validUntil <= config.expiresAt;

        vm.prank(relay);
        if (legal) {
            exchange.recordOffer(offer, signature);
            assertEq(exchange.getSession(config.sessionId).activeValidUntil, validUntil);
        } else {
            vm.expectRevert(INegotiationExchange.InvalidValidUntil.selector);
            exchange.recordOffer(offer, signature);
        }
    }

    /// @notice No key but the proposer's own produces a recordable offer.
    /// @dev The relay's key is one of the keys this covers. Gas payment and trading authority are
    ///      separate powers only for as long as this holds (docs/protocol.md section 1).
    function testFuzz_no_other_key_can_record_an_offer(uint256 wrongPk) public {
        wrongPk = bound(wrongPk, 1, type(uint128).max);
        vm.assume(vm.addr(wrongPk) != buyer);

        INegotiationExchange.SessionConfig memory config = openSession();

        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 92 * ONE_TOKEN);
        bytes memory signature = signOffer(wrongPk, offer);

        vm.prank(relay);
        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        exchange.recordOffer(offer, signature);
    }

    /// @notice A close is accepted on reasons 1..3 and refused on every other code.
    function testFuzz_close_reason_codes(uint8 reason) public {
        INegotiationExchange.SessionConfig memory config = openSession();

        INegotiationExchange.Close memory closure = buildClose(config, 1, buyer, reason);
        bytes memory signature = signClose(BUYER_PK, closure);

        vm.prank(relay);
        if (reason >= 1 && reason <= 3) {
            exchange.closeSession(closure, signature);
            assertEq(
                uint8(exchange.getSession(config.sessionId).status),
                uint8(INegotiationExchange.Status.Closed)
            );
        } else {
            vm.expectRevert(
                abi.encodeWithSelector(INegotiationExchange.InvalidReason.selector, reason)
            );
            exchange.closeSession(closure, signature);
        }
    }

    /// @notice An abort is accepted on reasons 1..4 and refused on every other code.
    function testFuzz_abort_reason_codes(uint8 reason) public {
        INegotiationExchange.SessionConfig memory config = openSession();

        vm.prank(operator);
        if (reason >= 1 && reason <= 4) {
            exchange.abortSession(config.sessionId, reason);
            assertEq(
                uint8(exchange.getSession(config.sessionId).status),
                uint8(INegotiationExchange.Status.Aborted)
            );
        } else {
            vm.expectRevert(
                abi.encodeWithSelector(INegotiationExchange.InvalidReason.selector, reason)
            );
            exchange.abortSession(config.sessionId, reason);
        }
    }

    /// @notice A deployment with the same address for both legs is refused, whatever it is.
    /// @dev Q17, ADR-037, as a property rather than the one case in `Deployment.t.sol`.
    function testFuzz_a_same_token_pair_is_always_refused(address token) public {
        vm.assume(token != address(0));

        vm.expectRevert(INegotiationExchange.InvalidTokenPair.selector);
        new NegotiationExchange(token, token, operator);
    }
}
