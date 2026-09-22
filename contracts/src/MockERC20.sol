// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @title Manufactured test token
/// @notice A plain ERC-20 with 6 decimals and operator-restricted minting, deployed twice: once
///         as mASSET (the base token) and once as mUSD (the quote token).
/// @dev docs/protocol.md section 2. Nothing here is a financial instrument and nothing here
///      carries value. Behaviour is deliberately ordinary: no rebasing, no fee on transfer, no
///      transfer callbacks, so `NegotiationExchange` may assume plain ERC-20 semantics.
///      Minting exists only so the operator can fund fresh test wallets before a run; it is
///      never reachable from settlement.
contract MockERC20 is ERC20 {
    /// @notice The only address permitted to mint. Immutable: there is no transfer of this role.
    address public immutable operator;

    uint8 private constant DECIMALS = 6;

    error NotOperator();

    constructor(string memory name_, string memory symbol_, address operator_)
        ERC20(name_, symbol_)
    {
        if (operator_ == address(0)) revert NotOperator();
        operator = operator_;
    }

    /// @notice Minor units per whole unit, matching the amounts used throughout the protocol.
    /// @dev docs/protocol.md section 2: every amount is an integer in minor units.
    function decimals() public pure override returns (uint8) {
        return DECIMALS;
    }

    /// @notice Create test tokens for a run's wallets.
    /// @dev Operator only, and called only during setup. The exchange never mints during
    ///      settlement: a settlement moves tokens that already exist or it reverts.
    function mint(address to, uint256 amount) external {
        if (msg.sender != operator) revert NotOperator();
        _mint(to, amount);
    }
}
