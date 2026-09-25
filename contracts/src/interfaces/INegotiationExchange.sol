// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

/// @title Negotiation exchange interface
/// @notice Types, errors and events for the two-party negotiation and settlement protocol.
///         This file is the machine-readable form of docs/protocol.md sections 3, 7, 8 and 9.
///         Every name here is canonical across Solidity, Python and TypeScript; renaming one
///         without the others fails the fixture test in packages/protocol.
interface INegotiationExchange {
    // ---------------------------------------------------------------------------------
    // Types — docs/protocol.md sections 3, 7 and 8
    // ---------------------------------------------------------------------------------

    /// @notice Lifecycle of one session. `None` is the default for an unknown sessionId.
    /// @dev docs/protocol.md section 7. Every non-`Open` value is terminal.
    enum Status {
        None,
        Open,
        Settled,
        Closed,
        Expired,
        Aborted
    }

    /// @notice The negotiation's fixed terms, set once by the operator at creation.
    /// @dev The two token addresses are deliberately absent: they are exchange immutables and
    ///      enter `configHash` from there, so a caller cannot name a different token.
    struct SessionConfig {
        bytes32 sessionId;
        address buyer;
        address seller;
        uint256 baseAmount;
        uint64 expiresAt;
        uint16 maxOffers;
    }

    /// @notice A signed proposal to exchange `baseAmount` of base for `quoteAmount` of quote.
    /// @dev An active offer is real authority: it permits the counterparty to execute that
    ///      trade until `validUntil` (docs/decision_log.md ADR-004).
    struct Offer {
        bytes32 sessionId;
        bytes32 configHash;
        uint64 sequence;
        address proposer;
        uint256 quoteAmount;
        uint64 validUntil;
    }

    /// @notice A signed acceptance of one specific offer digest.
    /// @dev `offerHash` is the full EIP-712 digest of the offer, domain included, so both
    ///      signatures authorise the same trade on the same chain and contract.
    struct Accept {
        bytes32 sessionId;
        bytes32 configHash;
        uint64 sequence;
        address actor;
        bytes32 offerHash;
    }

    /// @notice A signed walk-away. `reason` is one of the participant codes 1..3.
    struct Close {
        bytes32 sessionId;
        bytes32 configHash;
        uint64 sequence;
        address actor;
        uint8 reason;
    }

    /// @notice Everything the contract knows about one session.
    struct SessionState {
        SessionConfig config;
        bytes32 configHash;
        Status status;
        uint64 sequence;
        uint16 offerCount;
        bytes32 activeOfferHash;
        address activeProposer;
        uint256 activeQuoteAmount;
        uint64 activeValidUntil;
        uint64 activeSequence;
    }

    // ---------------------------------------------------------------------------------
    // Events — docs/protocol.md section 9
    //
    // These are the canonical record. The database is a projection of them and the
    // reconstruction tool reads nothing else (A15).
    // ---------------------------------------------------------------------------------

    event SessionOpened(
        bytes32 indexed sessionId,
        address indexed buyer,
        address indexed seller,
        address baseToken,
        address quoteToken,
        uint256 baseAmount,
        uint64 expiresAt,
        uint16 maxOffers,
        bytes32 configHash
    );

    event OfferRecorded(
        bytes32 indexed sessionId,
        uint64 sequence,
        address indexed proposer,
        uint256 quoteAmount,
        uint64 validUntil,
        bytes32 offerHash
    );

    event AcceptanceRecorded(
        bytes32 indexed sessionId, uint64 sequence, address indexed actor, bytes32 offerHash
    );

    event SettlementCompleted(
        bytes32 indexed sessionId,
        address buyer,
        address seller,
        address baseToken,
        uint256 baseAmount,
        address quoteToken,
        uint256 quoteAmount,
        bytes32 offerHash
    );

    event SessionClosed(
        bytes32 indexed sessionId, uint64 sequence, address indexed actor, uint8 reason
    );

    event SessionExpired(bytes32 indexed sessionId, uint64 expiresAt);

    event SessionAborted(bytes32 indexed sessionId, address indexed operator, uint8 reason);

    // ---------------------------------------------------------------------------------
    // Errors — docs/protocol.md section 8.3
    //
    // Custom errors rather than require strings, so the indexer decodes a revert into a
    // stable code and a failed action stays distinguishable from an economic outcome.
    // ---------------------------------------------------------------------------------

    error NotOperator();
    error SessionExists();
    error InvalidParties();
    error InvalidTokenPair();
    error InvalidBaseAmount();
    error InvalidExpiry();
    error InvalidMaxOffers();
    error SessionNotOpen(uint8 status);
    error SessionDeadlinePassed();
    error SessionNotExpired();
    error ConfigHashMismatch();
    error SequenceMismatch(uint64 expected, uint64 actual);
    error BadSignature();
    error NotParticipant(address signer);
    error NotProposerTurn(address expected);
    error OfferLimitReached(uint16 maxOffers);
    error InvalidQuoteAmount();
    error InvalidValidUntil();
    error NoActiveOffer();
    error StaleOfferDigest(bytes32 active, bytes32 given);
    error OfferExpired();
    error SelfAcceptance();
    error InvalidReason(uint8 reason);

    // ---------------------------------------------------------------------------------
    // Functions — docs/protocol.md section 8
    // ---------------------------------------------------------------------------------

    function createSession(SessionConfig calldata config) external;
    function recordOffer(Offer calldata offer, bytes calldata signature) external;
    function acceptAndSettle(Accept calldata acceptance, bytes calldata signature) external;
    function closeSession(Close calldata closure, bytes calldata signature) external;
    function expireSession(bytes32 sessionId) external;
    function abortSession(bytes32 sessionId, uint8 reason) external;

    function getSession(bytes32 sessionId) external view returns (SessionState memory);
    function hashOffer(Offer calldata offer) external view returns (bytes32);
    function hashAccept(Accept calldata acceptance) external view returns (bytes32);
    function hashClose(Close calldata closure) external view returns (bytes32);

    function baseToken() external view returns (address);
    function quoteToken() external view returns (address);
    function operator() external view returns (address);
}
