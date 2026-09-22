// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

import {BaseTest} from "../BaseTest.sol";
import {MockERC20} from "../../src/MockERC20.sol";
import {NegotiationExchange} from "../../src/NegotiationExchange.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice A signature is valid for exactly one chain, one contract, one session and one
///         configuration, and for nothing else.
/// @dev Acceptance A07. docs/protocol.md sections 3 and 4: EIP-712 alone does not stop replay;
///      the domain plus `configHash` plus `sessionId` plus sequence together do.
contract DomainTest is BaseTest {
    bytes32 private constant OFFER_TYPEHASH = keccak256(
        "Offer(bytes32 sessionId,bytes32 configHash,uint64 sequence,address proposer,uint256 quoteAmount,uint64 validUntil)"
    );

    INegotiationExchange.SessionConfig internal config;

    function setUp() public override {
        super.setUp();
        config = openSession();
    }

    /// @dev A signature made for a second deployment of the same code does not work here. The
    ///      domain separator includes `verifyingContract`, so the digest differs and recovery
    ///      yields some other address.
    function test_A07_a_signature_for_another_deployment_is_rejected() public {
        vm.startPrank(operator);
        NegotiationExchange other =
            new NegotiationExchange(address(baseToken), address(quoteToken), operator);
        other.createSession(config);
        vm.stopPrank();

        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        // Signed against the other exchange's domain.
        bytes memory foreignSignature = sign(BUYER_PK, other.hashOffer(offer));

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, foreignSignature);
    }

    /// @dev Forge a domain separator with a different chain id and sign against it.
    function test_A07_a_signature_for_another_chain_is_rejected() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);

        bytes32 foreignDomain = keccak256(
            abi.encode(
                keccak256(
                    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                ),
                keccak256(bytes("AgentNegotiationSandbox")),
                keccak256(bytes("1")),
                uint256(11_155_111), // Sepolia, while this chain is 31337
                address(exchange)
            )
        );
        bytes32 structHash = keccak256(
            abi.encode(
                OFFER_TYPEHASH,
                offer.sessionId,
                offer.configHash,
                offer.sequence,
                offer.proposer,
                offer.quoteAmount,
                offer.validUntil
            )
        );
        bytes memory foreignSignature =
            sign(BUYER_PK, MessageHashUtils.toTypedDataHash(foreignDomain, structHash));

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, foreignSignature);
    }

    /// @dev A forged domain name, everything else identical.
    function test_A07_a_signature_under_a_forged_domain_name_is_rejected() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);

        bytes32 forgedDomain = keccak256(
            abi.encode(
                keccak256(
                    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                ),
                keccak256(bytes("SomeOtherProtocol")),
                keccak256(bytes("1")),
                block.chainid,
                address(exchange)
            )
        );
        bytes32 structHash = keccak256(
            abi.encode(
                OFFER_TYPEHASH,
                offer.sessionId,
                offer.configHash,
                offer.sequence,
                offer.proposer,
                offer.quoteAmount,
                offer.validUntil
            )
        );
        bytes memory forged =
            sign(BUYER_PK, MessageHashUtils.toTypedDataHash(forgedDomain, structHash));

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, forged);
    }

    /// @dev An offer correctly signed for session A does not apply to session B, because the
    ///      session's own state is looked up by `sessionId` and its stored `configHash` differs.
    function test_A07_an_offer_for_another_session_is_rejected() public {
        INegotiationExchange.SessionConfig memory otherConfig = defaultConfig();
        otherConfig.sessionId = keccak256("session-2");
        vm.prank(operator);
        exchange.createSession(otherConfig);

        // A genuine, correctly signed offer for session 1 ...
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        bytes memory signature = signOffer(BUYER_PK, offer);

        // ... re-pointed at session 2. The configHash no longer matches that session's stored
        // value, so it is refused before any signature question arises.
        offer.sessionId = otherConfig.sessionId;

        vm.expectRevert(INegotiationExchange.ConfigHashMismatch.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    function test_A07_a_mismatched_config_hash_is_rejected() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        offer.configHash = keccak256("not the session configuration");
        bytes memory signature = signOffer(BUYER_PK, offer);

        vm.expectRevert(INegotiationExchange.ConfigHashMismatch.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    /// @dev Mutating any signed field after signing breaks recovery. This is the property the
    ///      relay's inability to alter terms rests on.
    function test_a_tampered_quote_amount_breaks_the_signature() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);
        bytes memory signature = signOffer(BUYER_PK, offer);

        offer.quoteAmount = 1 * ONE_TOKEN; // a relay trying to improve its own terms

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, signature);
    }

    function test_a_malformed_signature_is_rejected_not_reverted_by_the_library() public {
        INegotiationExchange.Offer memory offer = buildOffer(config, 1, buyer, 94 * ONE_TOKEN);

        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        vm.prank(relay);
        exchange.recordOffer(offer, hex"1234");
    }

    /// @dev The two mock tokens are constructor immutables, so a second exchange over a
    ///      different pair produces a different `configHash` for identical parameters.
    function test_config_hash_differs_across_exchanges_with_different_token_pairs() public {
        vm.startPrank(operator);
        MockERC20 otherBase = new MockERC20("Other", "OTHER", operator);
        NegotiationExchange other =
            new NegotiationExchange(address(otherBase), address(quoteToken), operator);
        other.createSession(config);
        vm.stopPrank();

        assertTrue(
            exchange.getSession(sessionId).configHash != other.getSession(sessionId).configHash,
            "token pair is bound into configHash"
        );
    }
}
