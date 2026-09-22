// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {INegotiationExchange} from "./interfaces/INegotiationExchange.sol";

/// @title NegotiationExchange
/// @notice Records a two-party price negotiation and settles the agreed exchange atomically.
/// @dev Implements docs/protocol.md. The contract is the authority: it decides which actions are
///      legal, and an agent that proposes something illegal gets a revert rather than a trade.
///
///      Three properties the rest of the system depends on:
///      - **An active offer is real authority.** Recording an offer permits the counterparty to
///        execute exactly that trade until it expires or is replaced (ADR-004).
///      - **Settlement is both legs or neither.** `acceptAndSettle` moves quote and base in one
///        transaction; any failure reverts everything, including the status change.
///      - **Terminal is terminal.** Settled, Closed, Expired and Aborted are permanent, and the
///        four are distinguishable in the event log, because the difference between them is the
///        experiment's result (docs/architecture.md section 1).
///
///      Non-upgradeable, no owner transfer, no pause, no callbacks, no permit path, no arbitrary
///      token or target parameters. The token pair is fixed at construction.
contract NegotiationExchange is INegotiationExchange, EIP712, ReentrancyGuard {
    using SafeERC20 for IERC20;

    /// @dev Byte-exact type strings from docs/protocol.md section 4. Changing one changes every
    ///      digest, so it is a protocol version bump and a new deployment (section 15).
    bytes32 private constant OFFER_TYPEHASH = keccak256(
        "Offer(bytes32 sessionId,bytes32 configHash,uint64 sequence,address proposer,uint256 quoteAmount,uint64 validUntil)"
    );
    bytes32 private constant ACCEPT_TYPEHASH = keccak256(
        "Accept(bytes32 sessionId,bytes32 configHash,uint64 sequence,address actor,bytes32 offerHash)"
    );
    bytes32 private constant CLOSE_TYPEHASH = keccak256(
        "Close(bytes32 sessionId,bytes32 configHash,uint64 sequence,address actor,uint8 reason)"
    );

    uint16 private constant MAX_OFFERS_LIMIT = 32;
    uint8 private constant MAX_CLOSE_REASON = 3;
    uint8 private constant MAX_ABORT_REASON = 4;

    /// @inheritdoc INegotiationExchange
    address public immutable baseToken;
    /// @inheritdoc INegotiationExchange
    address public immutable quoteToken;
    /// @inheritdoc INegotiationExchange
    address public immutable operator;

    mapping(bytes32 sessionId => SessionState state) private _sessions;

    /// @param baseToken_ mASSET, the fixed quantity the seller delivers.
    /// @param quoteToken_ mUSD, the negotiated amount the buyer pays.
    /// @param operator_ The only address that may create or abort a session.
    constructor(address baseToken_, address quoteToken_, address operator_)
        EIP712("AgentNegotiationSandbox", "1")
    {
        if (baseToken_ == address(0) || quoteToken_ == address(0) || operator_ == address(0)) {
            revert InvalidParties();
        }
        baseToken = baseToken_;
        quoteToken = quoteToken_;
        operator = operator_;
    }

    // ---------------------------------------------------------------------------------
    // Lifecycle
    // ---------------------------------------------------------------------------------

    /// @inheritdoc INegotiationExchange
    /// @dev docs/protocol.md section 3. The token addresses in `configHash` come from this
    ///      contract's immutables, never from the caller, so a session cannot be bound to a
    ///      token the exchange does not trade.
    function createSession(SessionConfig calldata config) external {
        if (msg.sender != operator) revert NotOperator();
        if (_sessions[config.sessionId].status != Status.None) revert SessionExists();
        if (config.buyer == address(0) || config.seller == address(0)) revert InvalidParties();
        if (config.buyer == config.seller) revert InvalidParties();
        if (config.baseAmount == 0) revert InvalidBaseAmount();
        if (config.expiresAt <= block.timestamp) revert InvalidExpiry();
        if (config.maxOffers == 0 || config.maxOffers > MAX_OFFERS_LIMIT) {
            revert InvalidMaxOffers();
        }

        bytes32 configHash = _computeConfigHash(config);

        SessionState storage session = _sessions[config.sessionId];
        session.config = config;
        session.configHash = configHash;
        session.status = Status.Open;

        emit SessionOpened(
            config.sessionId,
            config.buyer,
            config.seller,
            baseToken,
            quoteToken,
            config.baseAmount,
            config.expiresAt,
            config.maxOffers,
            configHash
        );
    }

    /// @inheritdoc INegotiationExchange
    /// @dev docs/protocol.md section 8.2. A new offer replaces the active one, and the replaced
    ///      digest becomes permanently unexecutable: `acceptAndSettle` compares against the
    ///      stored digest, so a counterparty racing an acceptance against a replacement gets
    ///      `StaleOfferDigest` rather than an unintended trade (A05).
    function recordOffer(Offer calldata offer, bytes calldata signature) external {
        SessionState storage session =
            _verifyCommon(offer.sessionId, offer.configHash, offer.sequence);

        _requireSigner(hashOffer(offer), signature, offer.proposer, session);

        address expectedProposer = _nextProposer(session);
        if (offer.proposer != expectedProposer) revert NotProposerTurn(expectedProposer);
        if (session.offerCount == session.config.maxOffers) {
            revert OfferLimitReached(session.config.maxOffers);
        }
        if (offer.quoteAmount == 0) revert InvalidQuoteAmount();
        if (offer.validUntil <= block.timestamp || offer.validUntil > session.config.expiresAt) {
            revert InvalidValidUntil();
        }

        bytes32 offerHash = hashOffer(offer);
        session.sequence = offer.sequence;
        session.offerCount += 1;
        session.activeOfferHash = offerHash;
        session.activeProposer = offer.proposer;
        session.activeQuoteAmount = offer.quoteAmount;
        session.activeValidUntil = offer.validUntil;
        session.activeSequence = offer.sequence;

        emit OfferRecorded(
            offer.sessionId,
            offer.sequence,
            offer.proposer,
            offer.quoteAmount,
            offer.validUntil,
            offerHash
        );
    }

    /// @inheritdoc INegotiationExchange
    /// @dev docs/protocol.md section 8.2. Effects land before the transfers, so a token that
    ///      reverts leaves no trace at all rather than a Settled session with no settlement.
    ///      Both transfers are `safeTransferFrom` against allowances the participants granted
    ///      during setup; the exchange never holds a balance and never mints.
    function acceptAndSettle(Accept calldata acceptance, bytes calldata signature)
        external
        nonReentrant
    {
        SessionState storage session =
            _verifyCommon(acceptance.sessionId, acceptance.configHash, acceptance.sequence);

        _requireSigner(hashAccept(acceptance), signature, acceptance.actor, session);

        bytes32 activeHash = session.activeOfferHash;
        if (activeHash == bytes32(0)) revert NoActiveOffer();
        if (acceptance.offerHash != activeHash) {
            revert StaleOfferDigest(activeHash, acceptance.offerHash);
        }
        if (block.timestamp >= session.activeValidUntil) revert OfferExpired();
        if (acceptance.actor == session.activeProposer) revert SelfAcceptance();

        address buyer = session.config.buyer;
        address seller = session.config.seller;
        uint256 baseAmount = session.config.baseAmount;
        uint256 quoteAmount = session.activeQuoteAmount;

        session.status = Status.Settled;
        session.sequence = acceptance.sequence;
        session.activeOfferHash = bytes32(0);
        session.activeProposer = address(0);
        session.activeQuoteAmount = 0;
        session.activeValidUntil = 0;
        session.activeSequence = 0;

        IERC20(quoteToken).safeTransferFrom(buyer, seller, quoteAmount);
        IERC20(baseToken).safeTransferFrom(seller, buyer, baseAmount);

        emit AcceptanceRecorded(
            acceptance.sessionId, acceptance.sequence, acceptance.actor, activeHash
        );
        emit SettlementCompleted(
            acceptance.sessionId,
            buyer,
            seller,
            baseToken,
            baseAmount,
            quoteToken,
            quoteAmount,
            activeHash
        );
    }

    /// @inheritdoc INegotiationExchange
    /// @dev Either participant may close while the session is Open, whether or not it is their
    ///      turn: a party that will not trade is not required to wait to say so.
    function closeSession(Close calldata closure, bytes calldata signature) external {
        SessionState storage session =
            _verifyCommon(closure.sessionId, closure.configHash, closure.sequence);

        _requireSigner(hashClose(closure), signature, closure.actor, session);

        if (closure.reason == 0 || closure.reason > MAX_CLOSE_REASON) {
            revert InvalidReason(closure.reason);
        }

        session.status = Status.Closed;
        session.sequence = closure.sequence;

        emit SessionClosed(closure.sessionId, closure.sequence, closure.actor, closure.reason);
    }

    /// @inheritdoc INegotiationExchange
    /// @dev Callable by anyone after the deadline. Expiry is a public fact about the chain
    ///      clock, not an operator privilege, so a session cannot be held open by inaction.
    function expireSession(bytes32 sessionId) external {
        SessionState storage session = _sessions[sessionId];
        if (session.status != Status.Open) revert SessionNotOpen(uint8(session.status));
        if (block.timestamp < session.config.expiresAt) revert SessionNotExpired();

        session.status = Status.Expired;

        emit SessionExpired(sessionId, session.config.expiresAt);
    }

    /// @inheritdoc INegotiationExchange
    /// @dev The operator's only power over a live session, and it is a visible one: an abort is
    ///      an event with a reason code, never silence. After `expiresAt` it reverts, so expiry
    ///      cannot be relabelled as an abort after the fact.
    function abortSession(bytes32 sessionId, uint8 reason) external {
        if (msg.sender != operator) revert NotOperator();

        SessionState storage session = _sessions[sessionId];
        if (session.status != Status.Open) revert SessionNotOpen(uint8(session.status));
        if (block.timestamp >= session.config.expiresAt) revert SessionDeadlinePassed();
        if (reason == 0 || reason > MAX_ABORT_REASON) revert InvalidReason(reason);

        session.status = Status.Aborted;

        emit SessionAborted(sessionId, msg.sender, reason);
    }

    // ---------------------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------------------

    /// @inheritdoc INegotiationExchange
    function getSession(bytes32 sessionId) external view returns (SessionState memory) {
        return _sessions[sessionId];
    }

    /// @inheritdoc INegotiationExchange
    function hashOffer(Offer calldata offer) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    OFFER_TYPEHASH,
                    offer.sessionId,
                    offer.configHash,
                    offer.sequence,
                    offer.proposer,
                    offer.quoteAmount,
                    offer.validUntil
                )
            )
        );
    }

    /// @inheritdoc INegotiationExchange
    function hashAccept(Accept calldata acceptance) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    ACCEPT_TYPEHASH,
                    acceptance.sessionId,
                    acceptance.configHash,
                    acceptance.sequence,
                    acceptance.actor,
                    acceptance.offerHash
                )
            )
        );
    }

    /// @inheritdoc INegotiationExchange
    function hashClose(Close calldata closure) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    CLOSE_TYPEHASH,
                    closure.sessionId,
                    closure.configHash,
                    closure.sequence,
                    closure.actor,
                    closure.reason
                )
            )
        );
    }

    // ---------------------------------------------------------------------------------
    // Internals
    // ---------------------------------------------------------------------------------

    /// @dev Steps 1 to 4 of the verification order in docs/protocol.md section 8.1, shared by
    ///      every signed entry point so the order cannot drift between them.
    function _verifyCommon(bytes32 sessionId, bytes32 configHash, uint64 sequence)
        private
        view
        returns (SessionState storage session)
    {
        session = _sessions[sessionId];
        if (session.status != Status.Open) revert SessionNotOpen(uint8(session.status));
        if (block.timestamp >= session.config.expiresAt) revert SessionDeadlinePassed();
        if (configHash != session.configHash) revert ConfigHashMismatch();

        uint64 expected = session.sequence + 1;
        if (sequence != expected) revert SequenceMismatch(expected, sequence);
    }

    /// @dev Steps 5 and 6: the recovered signer must be the address the message names, and that
    ///      address must be one of the two participants. The relay's key can satisfy neither,
    ///      which is what makes gas payment and trading authority separate powers.
    function _requireSigner(
        bytes32 digest,
        bytes calldata signature,
        address claimed,
        SessionState storage session
    ) private view {
        (address recovered, ECDSA.RecoverError err,) = ECDSA.tryRecover(digest, signature);
        if (err != ECDSA.RecoverError.NoError || recovered != claimed) revert BadSignature();
        if (claimed != session.config.buyer && claimed != session.config.seller) {
            revert NotParticipant(claimed);
        }
    }

    /// @dev docs/protocol.md rule 5.3. The buyer opens; afterwards it is the counterparty of the
    ///      most recent recorded offer. `activeProposer` is only cleared on settlement, which is
    ///      terminal, so it still names the last proposer when an offer has merely expired —
    ///      which is the case the rule calls out explicitly.
    function _nextProposer(SessionState storage session) private view returns (address) {
        if (session.offerCount == 0) return session.config.buyer;
        return session.activeProposer == session.config.buyer
            ? session.config.seller
            : session.config.buyer;
    }

    /// @dev docs/protocol.md section 3. `abi.encode`, never concatenation, so every field is
    ///      padded to 32 bytes and no two configurations can collide by running together.
    function _computeConfigHash(SessionConfig calldata config) private view returns (bytes32) {
        return keccak256(
            abi.encode(
                config.sessionId,
                config.buyer,
                config.seller,
                baseToken,
                quoteToken,
                config.baseAmount,
                config.expiresAt,
                config.maxOffers
            )
        );
    }
}
