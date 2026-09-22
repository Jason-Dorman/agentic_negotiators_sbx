// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {BaseTest} from "../BaseTest.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice `createSession` validation, events and `configHash`.
/// @dev docs/protocol.md section 3; docs/test_strategy.md section 4.1.
contract CreateSessionTest is BaseTest {
    function test_operator_opens_a_session_and_emits_the_configuration() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        bytes32 configHash = expectedConfigHash(config);

        vm.expectEmit(true, true, true, true, address(exchange));
        emit INegotiationExchange.SessionOpened(
            config.sessionId,
            buyer,
            seller,
            address(baseToken),
            address(quoteToken),
            BASE_AMOUNT,
            config.expiresAt,
            MAX_OFFERS,
            configHash
        );

        vm.prank(operator);
        exchange.createSession(config);

        INegotiationExchange.SessionState memory state = exchange.getSession(sessionId);
        assertEq(uint8(state.status), uint8(INegotiationExchange.Status.Open), "status");
        assertEq(state.configHash, configHash, "configHash matches the section 3 encoding");
        assertEq(state.sequence, 0, "sequence starts at zero");
        assertEq(state.offerCount, 0, "offerCount starts at zero");
        assertEq(state.activeOfferHash, bytes32(0), "no active offer");
    }

    /// @dev The token addresses in `configHash` come from the exchange's immutables. Were they
    ///      taken from the caller, an operator could bind a session to a token the exchange
    ///      does not trade, and the agents' recomputation would still agree.
    function test_configHash_binds_the_exchange_token_pair_not_caller_supplied_addresses() public {
        INegotiationExchange.SessionConfig memory config = openSession();

        bytes32 withOtherTokens = keccak256(
            abi.encode(
                config.sessionId,
                config.buyer,
                config.seller,
                address(0xDEAD),
                address(0xBEEF),
                config.baseAmount,
                config.expiresAt,
                config.maxOffers
            )
        );

        assertTrue(exchange.getSession(sessionId).configHash != withOtherTokens);
    }

    function test_non_operator_cannot_create() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        vm.expectRevert(INegotiationExchange.NotOperator.selector);
        vm.prank(stranger);
        exchange.createSession(config);
    }

    function test_duplicate_sessionId_reverts() public {
        openSession();
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        vm.expectRevert(INegotiationExchange.SessionExists.selector);
        vm.prank(operator);
        exchange.createSession(config);
    }

    /// @dev A settled session's id stays used: `SessionExists` is checked on status, and every
    ///      terminal status is non-`None`.
    function test_sessionId_cannot_be_reused_after_a_terminal_status() public {
        INegotiationExchange.SessionConfig memory config = openSession();
        vm.prank(operator);
        exchange.abortSession(sessionId, 1);

        vm.expectRevert(INegotiationExchange.SessionExists.selector);
        vm.prank(operator);
        exchange.createSession(config);
    }

    function test_zero_buyer_reverts() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.buyer = address(0);
        _expectCreateRevert(config, INegotiationExchange.InvalidParties.selector);
    }

    function test_zero_seller_reverts() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.seller = address(0);
        _expectCreateRevert(config, INegotiationExchange.InvalidParties.selector);
    }

    function test_identical_parties_revert() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.seller = config.buyer;
        _expectCreateRevert(config, INegotiationExchange.InvalidParties.selector);
    }

    function test_zero_base_amount_reverts() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.baseAmount = 0;
        _expectCreateRevert(config, INegotiationExchange.InvalidBaseAmount.selector);
    }

    function test_expiry_at_or_before_now_reverts() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.expiresAt = uint64(block.timestamp);
        _expectCreateRevert(config, INegotiationExchange.InvalidExpiry.selector);
    }

    function test_zero_max_offers_reverts() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.maxOffers = 0;
        _expectCreateRevert(config, INegotiationExchange.InvalidMaxOffers.selector);
    }

    function test_max_offers_above_thirty_two_reverts() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.maxOffers = 33;
        _expectCreateRevert(config, INegotiationExchange.InvalidMaxOffers.selector);
    }

    function test_max_offers_boundaries_are_inclusive() public {
        INegotiationExchange.SessionConfig memory config = defaultConfig();
        config.maxOffers = 1;
        vm.prank(operator);
        exchange.createSession(config);

        config.sessionId = keccak256("session-32");
        config.maxOffers = 32;
        vm.prank(operator);
        exchange.createSession(config);

        assertEq(exchange.getSession(keccak256("session-32")).config.maxOffers, 32);
    }

    function _expectCreateRevert(INegotiationExchange.SessionConfig memory config, bytes4 selector)
        private
    {
        vm.expectRevert(selector);
        vm.prank(operator);
        exchange.createSession(config);
    }
}
