// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";

import {MockERC20} from "../../src/MockERC20.sol";

/// @notice The manufactured test token: 6 decimals, operator-only minting, nothing else.
/// @dev docs/protocol.md section 2.
contract MockERC20Test is Test {
    address internal operator = makeAddr("operator");
    address internal stranger = makeAddr("stranger");
    address internal holder = makeAddr("holder");

    MockERC20 internal token;

    function setUp() public {
        token = new MockERC20("Mock USD", "mUSD", operator);
    }

    function test_metadata_matches_the_protocol() public view {
        assertEq(token.name(), "Mock USD");
        assertEq(token.symbol(), "mUSD");
        assertEq(token.decimals(), 6, "minor units per whole unit");
        assertEq(token.operator(), operator);
    }

    function test_operator_can_mint() public {
        vm.prank(operator);
        token.mint(holder, 1_000_000);
        assertEq(token.balanceOf(holder), 1_000_000);
        assertEq(token.totalSupply(), 1_000_000);
    }

    /// @dev Minting is a setup power. If anyone could mint, the settlement's exact-delta
    ///      guarantee would say nothing about whether a party could afford the trade.
    function test_only_the_operator_can_mint() public {
        vm.expectRevert(MockERC20.NotOperator.selector);
        vm.prank(stranger);
        token.mint(stranger, 1);
    }

    function test_the_operator_role_is_immutable_and_cannot_be_zero() public {
        vm.expectRevert(MockERC20.NotOperator.selector);
        new MockERC20("Bad", "BAD", address(0));
    }

    function testFuzz_minting_is_additive(uint128 first, uint128 second) public {
        vm.startPrank(operator);
        token.mint(holder, first);
        token.mint(holder, second);
        vm.stopPrank();

        assertEq(token.balanceOf(holder), uint256(first) + uint256(second));
    }
}
