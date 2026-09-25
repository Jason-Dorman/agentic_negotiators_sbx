// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";

import {MockERC20} from "../src/MockERC20.sol";
import {NegotiationExchange} from "../src/NegotiationExchange.sol";
import {INegotiationExchange} from "../src/interfaces/INegotiationExchange.sol";

/// @title Shared fixture for the contract suite
/// @notice Deploys the pair of mock tokens and the exchange, funds two fresh participants, and
///         provides signing helpers so a test reads as the protocol action it is exercising.
/// @dev The signing helpers construct the typed message from arguments and sign with a known
///      key. That is the opposite of the production rule — there the signer builds the message
///      from validated state and never from supplied bytes — and it is correct here: these
///      tests must be able to construct messages the production signer would refuse, in order
///      to prove the contract refuses them too.
abstract contract BaseTest is Test {
    uint256 internal constant BUYER_PK = 0xB0B;
    uint256 internal constant SELLER_PK = 0x5E11E4;
    uint256 internal constant STRANGER_PK = 0x57A;

    /// @dev 6 decimals, per docs/protocol.md section 2.
    uint256 internal constant ONE_TOKEN = 1e6;
    uint256 internal constant BASE_AMOUNT = 10 * ONE_TOKEN;
    uint256 internal constant BUYER_QUOTE_BALANCE = 250 * ONE_TOKEN;
    uint16 internal constant MAX_OFFERS = 8;
    uint64 internal constant SESSION_DURATION = 1800;
    uint64 internal constant OFFER_LIFETIME = 600;

    address internal buyer;
    address internal seller;
    address internal stranger;
    address internal operator = makeAddr("operator");
    address internal relay = makeAddr("relay");

    MockERC20 internal baseToken;
    MockERC20 internal quoteToken;
    NegotiationExchange internal exchange;

    bytes32 internal sessionId = keccak256("session-1");

    function setUp() public virtual {
        buyer = vm.addr(BUYER_PK);
        seller = vm.addr(SELLER_PK);
        stranger = vm.addr(STRANGER_PK);

        // A timestamp well clear of zero, so `expiresAt` arithmetic is never near an underflow
        // and `vm.warp` backwards stays legal within a test.
        vm.warp(1_760_000_000);

        vm.startPrank(operator);
        baseToken = new MockERC20("Mock Asset", "mASSET", operator);
        quoteToken = new MockERC20("Mock USD", "mUSD", operator);
        exchange = new NegotiationExchange(address(baseToken), address(quoteToken), operator);

        baseToken.mint(seller, BASE_AMOUNT);
        quoteToken.mint(buyer, BUYER_QUOTE_BALANCE);
        vm.stopPrank();

        vm.prank(buyer);
        quoteToken.approve(address(exchange), type(uint256).max);
        vm.prank(seller);
        baseToken.approve(address(exchange), type(uint256).max);
    }

    // ---------------------------------------------------------------------------------
    // Session helpers
    // ---------------------------------------------------------------------------------

    function defaultConfig() internal view returns (INegotiationExchange.SessionConfig memory) {
        return INegotiationExchange.SessionConfig({
            sessionId: sessionId,
            buyer: buyer,
            seller: seller,
            baseAmount: BASE_AMOUNT,
            expiresAt: uint64(block.timestamp) + SESSION_DURATION,
            maxOffers: MAX_OFFERS
        });
    }

    function openSession() internal returns (INegotiationExchange.SessionConfig memory config) {
        config = defaultConfig();
        vm.prank(operator);
        exchange.createSession(config);
    }

    /// @notice Recompute `configHash` off-chain, exactly as docs/protocol.md section 3 defines
    ///         it, rather than reading it back from the contract.
    /// @dev Reading it back would make the tests agree with the implementation by construction.
    ///      Computing it here means a change to the encoding fails the suite.
    function expectedConfigHash(INegotiationExchange.SessionConfig memory config)
        internal
        view
        returns (bytes32)
    {
        return keccak256(
            abi.encode(
                config.sessionId,
                config.buyer,
                config.seller,
                address(baseToken),
                address(quoteToken),
                config.baseAmount,
                config.expiresAt,
                config.maxOffers
            )
        );
    }

    // ---------------------------------------------------------------------------------
    // Message construction and signing
    // ---------------------------------------------------------------------------------

    function buildOffer(
        INegotiationExchange.SessionConfig memory config,
        uint64 sequence,
        address proposer,
        uint256 quoteAmount
    ) internal view returns (INegotiationExchange.Offer memory) {
        uint64 validUntil = uint64(vm.getBlockTimestamp()) + OFFER_LIFETIME;
        if (validUntil > config.expiresAt) validUntil = config.expiresAt;
        return INegotiationExchange.Offer({
            sessionId: config.sessionId,
            configHash: expectedConfigHash(config),
            sequence: sequence,
            proposer: proposer,
            quoteAmount: quoteAmount,
            validUntil: validUntil
        });
    }

    function sign(uint256 privateKey, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function signOffer(uint256 privateKey, INegotiationExchange.Offer memory offer)
        internal
        view
        returns (bytes memory)
    {
        return sign(privateKey, exchange.hashOffer(offer));
    }

    function signAccept(uint256 privateKey, INegotiationExchange.Accept memory acceptance)
        internal
        view
        returns (bytes memory)
    {
        return sign(privateKey, exchange.hashAccept(acceptance));
    }

    function signClose(uint256 privateKey, INegotiationExchange.Close memory closure)
        internal
        view
        returns (bytes memory)
    {
        return sign(privateKey, exchange.hashClose(closure));
    }

    // ---------------------------------------------------------------------------------
    // Composite actions
    // ---------------------------------------------------------------------------------

    /// @notice Record one offer from `proposer`, submitted by the relay as in production.
    function recordOfferAs(
        INegotiationExchange.SessionConfig memory config,
        uint64 sequence,
        address proposer,
        uint256 privateKey,
        uint256 quoteAmount
    ) internal returns (INegotiationExchange.Offer memory offer, bytes32 offerHash) {
        offer = buildOffer(config, sequence, proposer, quoteAmount);
        bytes memory signature = signOffer(privateKey, offer);
        offerHash = exchange.hashOffer(offer);
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    function buildAccept(
        INegotiationExchange.SessionConfig memory config,
        uint64 sequence,
        address actor,
        bytes32 offerHash
    ) internal view returns (INegotiationExchange.Accept memory) {
        return INegotiationExchange.Accept({
            sessionId: config.sessionId,
            configHash: expectedConfigHash(config),
            sequence: sequence,
            actor: actor,
            offerHash: offerHash
        });
    }

    function buildClose(
        INegotiationExchange.SessionConfig memory config,
        uint64 sequence,
        address actor,
        uint8 reason
    ) internal view returns (INegotiationExchange.Close memory) {
        return INegotiationExchange.Close({
            sessionId: config.sessionId,
            configHash: expectedConfigHash(config),
            sequence: sequence,
            actor: actor,
            reason: reason
        });
    }
}
