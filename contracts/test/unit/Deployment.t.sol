// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";

import {MockERC20} from "../../src/MockERC20.sol";
import {NegotiationExchange} from "../../src/NegotiationExchange.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice Constructor wiring: the three addresses an exchange is permanently bound to.
/// @dev These are immutables with no setter, so a mis-wired deployment cannot be corrected —
///      only replaced. The setup validator compares a live deployment against the manifest
///      ([architecture.md](../../docs/architecture.md) section 3.2, acceptance A17); this is
///      the constructor's own half of that guarantee.
contract DeploymentTest is Test {
    address internal operator = makeAddr("operator");

    MockERC20 internal baseToken;
    MockERC20 internal quoteToken;

    function setUp() public {
        vm.startPrank(operator);
        baseToken = new MockERC20("Mock Asset", "mASSET", operator);
        quoteToken = new MockERC20("Mock USD", "mUSD", operator);
        vm.stopPrank();
    }

    function test_a_zero_base_token_is_rejected() public {
        vm.expectRevert(INegotiationExchange.InvalidParties.selector);
        new NegotiationExchange(address(0), address(quoteToken), operator);
    }

    function test_a_zero_quote_token_is_rejected() public {
        vm.expectRevert(INegotiationExchange.InvalidParties.selector);
        new NegotiationExchange(address(baseToken), address(0), operator);
    }

    /// @dev A zero operator would leave a contract on which no session can ever be opened,
    ///      because `createSession` compares `msg.sender` against it.
    function test_a_zero_operator_is_rejected() public {
        vm.expectRevert(INegotiationExchange.InvalidParties.selector);
        new NegotiationExchange(address(baseToken), address(quoteToken), address(0));
    }

    /// @dev Q17, ADR-037. A same-token deployment would settle by moving `quoteAmount` from
    ///      buyer to seller and `baseAmount` back in the *same* token — a net payment at a price
    ///      neither party signed, which the event log records as an ordinary settlement. The
    ///      failure is silent and looks like a successful run in the evidence, so the
    ///      constructor refuses it rather than leaving the deploy script as the only guard.
    function test_a_same_token_pair_is_rejected() public {
        vm.expectRevert(INegotiationExchange.InvalidTokenPair.selector);
        new NegotiationExchange(address(baseToken), address(baseToken), operator);
    }

    function test_a_correctly_wired_deployment_exposes_its_immutables() public {
        NegotiationExchange exchange =
            new NegotiationExchange(address(baseToken), address(quoteToken), operator);

        assertEq(exchange.baseToken(), address(baseToken));
        assertEq(exchange.quoteToken(), address(quoteToken));
        assertEq(exchange.operator(), operator);
    }
}
