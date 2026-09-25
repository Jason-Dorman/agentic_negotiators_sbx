// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";

import {MockERC20} from "../../src/MockERC20.sol";
import {NegotiationExchange} from "../../src/NegotiationExchange.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice The Solidity third of the three-language digest check.
/// @dev docs/test_strategy.md section 5. `packages/protocol/fixtures/eip712.v1.json` is written by
///      a Python script that computes every digest by hand from docs/protocol.md sections 3 and 4.
///      pytest checks it with `eth_account`'s EIP-712 implementation, Vitest with viem's, and this
///      file with OpenZeppelin's, through the contract's own `hashOffer`, `hashAccept` and
///      `hashClose`. Four implementations agreeing is what makes the field names and types in three
///      languages more than a convention.
///
///      **The fixture's own addresses are used, not this test's.** The domain separator includes
///      `chainId` and `verifyingContract`, so a digest can only be reproduced by a contract that
///      actually lives at the fixture's address on the fixture's chain. `vm.chainId` and
///      `vm.deployCodeTo` put it there. Deploying anywhere else would have meant comparing this
///      contract's digest against a fixture computed for a different one, and the only way to make
///      that pass is to read the expected value out of the contract — which proves nothing.
///
///      This file goes further than digest equality: it replays the fixture's Offer and Accept
///      **with the fixture's own signatures**, through the relay, and asserts the settlement moved
///      the signed amounts. A fixture whose digests matched but whose signatures the contract
///      refused would be a fixture of an action that cannot happen.
contract FixturesTest is Test {
    string internal constant FIXTURE_PATH = "../packages/protocol/fixtures/eip712.v1.json";

    string internal fixture;

    address internal buyer;
    address internal seller;
    address internal operator;
    uint256 internal buyerPk;
    uint256 internal sellerPk;

    MockERC20 internal baseToken;
    MockERC20 internal quoteToken;
    NegotiationExchange internal exchange;

    INegotiationExchange.SessionConfig internal config;

    function setUp() public {
        fixture = vm.readFile(FIXTURE_PATH);

        // Every read below names its JSON key. That is the point of reading field by field rather
        // than ABI-decoding the whole object: a key renamed in the fixture fails here by name.
        buyer = vm.parseJsonAddress(fixture, "$.participants.buyer.address");
        seller = vm.parseJsonAddress(fixture, "$.participants.seller.address");
        operator = vm.parseJsonAddress(fixture, "$.deployment.operator");
        buyerPk = vm.parseJsonUint(fixture, "$.participants.buyer.private_key");
        sellerPk = vm.parseJsonUint(fixture, "$.participants.seller.private_key");

        vm.chainId(vm.parseJsonUint(fixture, "$.domain.chain_id"));
        vm.warp(vm.parseJsonUint(fixture, "$.chain_time"));

        address baseTokenAddress = vm.parseJsonAddress(fixture, "$.deployment.base_token");
        address quoteTokenAddress = vm.parseJsonAddress(fixture, "$.deployment.quote_token");
        address exchangeAddress = vm.parseJsonAddress(fixture, "$.domain.verifying_contract");

        deployCodeTo(
            "MockERC20.sol:MockERC20",
            abi.encode("Mock Asset", "mASSET", operator),
            baseTokenAddress
        );
        deployCodeTo(
            "MockERC20.sol:MockERC20", abi.encode("Mock USD", "mUSD", operator), quoteTokenAddress
        );
        deployCodeTo(
            "NegotiationExchange.sol:NegotiationExchange",
            abi.encode(baseTokenAddress, quoteTokenAddress, operator),
            exchangeAddress
        );

        baseToken = MockERC20(baseTokenAddress);
        quoteToken = MockERC20(quoteTokenAddress);
        exchange = NegotiationExchange(exchangeAddress);

        config = INegotiationExchange.SessionConfig({
            sessionId: vm.parseJsonBytes32(fixture, "$.session_config.session_id"),
            buyer: vm.parseJsonAddress(fixture, "$.session_config.buyer"),
            seller: vm.parseJsonAddress(fixture, "$.session_config.seller"),
            baseAmount: vm.parseJsonUint(fixture, "$.session_config.base_amount"),
            expiresAt: uint64(vm.parseJsonUint(fixture, "$.session_config.expires_at")),
            maxOffers: uint16(vm.parseJsonUint(fixture, "$.session_config.max_offers"))
        });
    }

    // ---------------------------------------------------------------------------------
    // Identity of the deployment the fixture was computed for
    // ---------------------------------------------------------------------------------

    function test_the_fixture_deployment_is_reproduced_exactly() public view {
        // If any of these were wrong, every digest assertion below would fail for a reason that
        // had nothing to do with the protocol. Asserting them separately keeps the diagnosis short.
        assertEq(address(exchange), vm.parseJsonAddress(fixture, "$.domain.verifying_contract"));
        assertEq(exchange.baseToken(), vm.parseJsonAddress(fixture, "$.deployment.base_token"));
        assertEq(exchange.quoteToken(), vm.parseJsonAddress(fixture, "$.deployment.quote_token"));
        assertEq(exchange.operator(), operator);
        assertEq(block.chainid, vm.parseJsonUint(fixture, "$.domain.chain_id"));
        assertEq(vm.addr(buyerPk), buyer);
        assertEq(vm.addr(sellerPk), seller);
    }

    /// @dev ERC-5267. The four domain fields are what bind a signature to this chain and this
    ///      contract, and the separator recomputed from them must equal the fixture's — otherwise
    ///      the digests below would agree only by coincidence.
    function test_the_domain_separator_matches_the_fixture() public view {
        (
            ,
            string memory name,
            string memory version,
            uint256 chainId,
            address verifyingContract,,
        ) = exchange.eip712Domain();

        assertEq(name, vm.parseJsonString(fixture, "$.domain.name"));
        assertEq(version, vm.parseJsonString(fixture, "$.domain.version"));
        assertEq(chainId, vm.parseJsonUint(fixture, "$.domain.chain_id"));
        assertEq(verifyingContract, vm.parseJsonAddress(fixture, "$.domain.verifying_contract"));

        bytes32 separator = keccak256(
            abi.encode(
                keccak256(
                    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                ),
                keccak256(bytes(name)),
                keccak256(bytes(version)),
                chainId,
                verifyingContract
            )
        );
        assertEq(separator, vm.parseJsonBytes32(fixture, "$.domain_separator"));
    }

    // ---------------------------------------------------------------------------------
    // configHash
    // ---------------------------------------------------------------------------------

    function test_the_config_hash_matches_the_fixture() public {
        vm.prank(operator);
        exchange.createSession(config);

        assertEq(
            exchange.getSession(config.sessionId).configHash,
            vm.parseJsonBytes32(fixture, "$.config_hash"),
            "configHash disagrees with the fixture: docs/protocol.md section 3"
        );
    }

    // ---------------------------------------------------------------------------------
    // Digests
    // ---------------------------------------------------------------------------------

    function test_the_offer_digest_matches_the_fixture() public view {
        assertEq(exchange.hashOffer(_fixtureOffer()), _digest("offer"));
    }

    function test_the_accept_digest_matches_the_fixture() public view {
        assertEq(exchange.hashAccept(_fixtureAccept()), _digest("accept"));
    }

    function test_the_close_digest_matches_the_fixture() public view {
        assertEq(exchange.hashClose(_fixtureClose()), _digest("close"));
    }

    function test_the_acceptance_references_the_offers_full_digest() public view {
        // docs/protocol.md section 4: `offerHash` is the whole Offer digest, domain included, so
        // both signatures authorise the same trade on the same chain and the same contract.
        assertEq(_fixtureAccept().offerHash, exchange.hashOffer(_fixtureOffer()));
    }

    // ---------------------------------------------------------------------------------
    // The fixture's signatures are accepted by the contract
    // ---------------------------------------------------------------------------------

    /// @dev The strongest form of this check. Digest equality says the three languages hash the
    ///      same bytes; this says the bytes are an executable negotiation.
    function test_the_fixture_offer_and_acceptance_settle_with_their_own_signatures() public {
        vm.prank(operator);
        exchange.createSession(config);

        vm.startPrank(operator);
        baseToken.mint(seller, config.baseAmount);
        quoteToken.mint(buyer, _fixtureOffer().quoteAmount);
        vm.stopPrank();

        vm.prank(buyer);
        quoteToken.approve(address(exchange), type(uint256).max);
        vm.prank(seller);
        baseToken.approve(address(exchange), type(uint256).max);

        address relay = makeAddr("fixture-relay");

        vm.prank(relay);
        exchange.recordOffer(_fixtureOffer(), _signature("offer"));

        vm.prank(relay);
        exchange.acceptAndSettle(_fixtureAccept(), _signature("accept"));

        assertEq(baseToken.balanceOf(buyer), config.baseAmount, "base leg");
        assertEq(quoteToken.balanceOf(seller), _fixtureOffer().quoteAmount, "quote leg");
        assertEq(
            uint8(exchange.getSession(config.sessionId).status),
            uint8(INegotiationExchange.Status.Settled)
        );
        assertEq(quoteToken.balanceOf(relay), 0, "the submitter received nothing");
    }

    /// @dev The fixture's Accept and Close are alternative continuations of the same negotiation:
    ///      both carry sequence 2, because both are the seller's reply to the offer at sequence 1.
    ///      So the Close branch is replayed on its own session, after the same offer — which is
    ///      exactly the shape of a walk-away run, and exercises rule 5.4: either participant may
    ///      close while the session is Open, whether or not it is their turn.
    function test_the_fixture_close_is_accepted_with_its_own_signature() public {
        vm.prank(operator);
        exchange.createSession(config);

        address relay = makeAddr("fixture-relay");

        vm.prank(relay);
        exchange.recordOffer(_fixtureOffer(), _signature("offer"));

        vm.prank(relay);
        exchange.closeSession(_fixtureClose(), _signature("close"));

        assertEq(
            uint8(exchange.getSession(config.sessionId).status),
            uint8(INegotiationExchange.Status.Closed)
        );
        assertEq(
            exchange.getSession(config.sessionId).sequence, _fixtureClose().sequence, "sequence"
        );
    }

    /// @dev The negative half: the fixture pins a digest, and the digest is what authorises. A
    ///      contract that hashed every message to the same value would pass every assertion above.
    function test_a_signature_over_a_different_amount_is_refused() public {
        vm.prank(operator);
        exchange.createSession(config);

        INegotiationExchange.Offer memory tampered = _fixtureOffer();
        tampered.quoteAmount += 1;

        bytes memory signature = _signature("offer");

        vm.prank(makeAddr("fixture-relay"));
        vm.expectRevert(INegotiationExchange.BadSignature.selector);
        exchange.recordOffer(tampered, signature);
    }

    // ---------------------------------------------------------------------------------
    // Fixture readers
    // ---------------------------------------------------------------------------------

    function _fixtureOffer() internal view returns (INegotiationExchange.Offer memory) {
        return INegotiationExchange.Offer({
            sessionId: vm.parseJsonBytes32(fixture, "$.messages.offer.message.session_id"),
            configHash: vm.parseJsonBytes32(fixture, "$.messages.offer.message.config_hash"),
            sequence: uint64(vm.parseJsonUint(fixture, "$.messages.offer.message.sequence")),
            proposer: vm.parseJsonAddress(fixture, "$.messages.offer.message.proposer"),
            quoteAmount: vm.parseJsonUint(fixture, "$.messages.offer.message.quote_amount"),
            validUntil: uint64(vm.parseJsonUint(fixture, "$.messages.offer.message.valid_until"))
        });
    }

    function _fixtureAccept() internal view returns (INegotiationExchange.Accept memory) {
        return INegotiationExchange.Accept({
            sessionId: vm.parseJsonBytes32(fixture, "$.messages.accept.message.session_id"),
            configHash: vm.parseJsonBytes32(fixture, "$.messages.accept.message.config_hash"),
            sequence: uint64(vm.parseJsonUint(fixture, "$.messages.accept.message.sequence")),
            actor: vm.parseJsonAddress(fixture, "$.messages.accept.message.actor"),
            offerHash: vm.parseJsonBytes32(fixture, "$.messages.accept.message.offer_hash")
        });
    }

    function _fixtureClose() internal view returns (INegotiationExchange.Close memory) {
        return INegotiationExchange.Close({
            sessionId: vm.parseJsonBytes32(fixture, "$.messages.close.message.session_id"),
            configHash: vm.parseJsonBytes32(fixture, "$.messages.close.message.config_hash"),
            sequence: uint64(vm.parseJsonUint(fixture, "$.messages.close.message.sequence")),
            actor: vm.parseJsonAddress(fixture, "$.messages.close.message.actor"),
            reason: uint8(vm.parseJsonUint(fixture, "$.messages.close.message.reason"))
        });
    }

    function _digest(string memory kind) internal view returns (bytes32) {
        return vm.parseJsonBytes32(fixture, string.concat("$.messages.", kind, ".digest"));
    }

    function _signature(string memory kind) internal view returns (bytes memory) {
        return vm.parseJsonBytes(fixture, string.concat("$.messages.", kind, ".signature"));
    }
}
