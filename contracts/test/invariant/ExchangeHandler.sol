// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";

import {MockERC20} from "../../src/MockERC20.sol";
import {NegotiationExchange} from "../../src/NegotiationExchange.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @title Fuzzed actor for the invariant suite
/// @notice Drives `NegotiationExchange` with random valid and invalid actions across several
///         sessions, and keeps the ghost state the invariants are checked against.
/// @dev docs/test_strategy.md section 4.2. Two rules shape everything here.
///
///      **Ghost state moves only on success.** Every entry point wraps the call in
///      `try`/`catch`, so the ghost record counts what the chain accepted rather than what the
///      fuzzer attempted. An invariant that compared intentions with state would pass for the
///      wrong reason the first time a revert was mis-predicted.
///
///      **Each session owns its wallets.** Session `i` has buyer `i` and seller `i` and they
///      appear in no other session, so a balance delta is attributable to one settlement. With
///      shared wallets, conservation would still hold while per-session amounts silently
///      crossed over, and invariant 5 would be untestable.
contract ExchangeHandler is Test {
    /// @dev Wallet pairs in the pool. The first `PRE_OPENED` have a session from `setUp`; the
    ///      rest are left for the fuzzer's own `createSession` calls, so session creation is
    ///      exercised under fuzzing without giving two sessions the same participants.
    uint256 internal constant SLOTS = 7;
    uint256 internal constant PRE_OPENED = 3;

    /// @dev Slot 6 is opened with a short life and closed immediately, so the campaign begins with a
    ///      session that is both **terminal** and, after the first time advance, **past its
    ///      deadline**. That is the one state from which `expireSession` could re-mark a finished
    ///      session, and without seeding it the mutation that deletes `expireSession`'s status
    ///      check was caught on five campaigns out of six — the fuzzer had to terminate a session
    ///      *and* push time past its expiry *and* then call `expireSession` on that same session.
    uint256 internal constant TERMINAL_SLOT = 6;
    uint64 internal constant SHORT_SESSION = 120;
    uint16 internal constant DEFAULT_MAX_OFFERS = 8;

    uint256 internal constant ONE_TOKEN = 1e6;
    uint256 internal constant BASE_AMOUNT = 10 * ONE_TOKEN;
    uint256 internal constant BUYER_QUOTE_BALANCE = 250 * ONE_TOKEN;
    uint256 internal constant SELLER_BASE_BALANCE = 25 * ONE_TOKEN;
    uint64 internal constant SESSION_DURATION = 1800;
    uint64 internal constant OFFER_LIFETIME = 600;

    /// @dev Deliberately uneven, and the first two are deliberately tiny. With `maxOffers = 8`
    ///      everywhere, reaching `OfferLimitReached` needs nine accepted offers in one session,
    ///      which a 64-call run spread over six sessions essentially never produces — so
    ///      invariant 3 was unfalsifiable and deleting the limit check survived mutation. One
    ///      and two also cover the `n == 1` boundary of the deterministic policy's own
    ///      arithmetic (docs/protocol.md section 13).
    function _maxOffersFor(uint256 slot) private pure returns (uint16) {
        if (slot == 0) return 1;
        if (slot == 1) return 2;
        return 8;
    }

    /// @notice Per-session record: the configuration, its wallets, and the ghost counters.
    struct Book {
        bool opened;
        bytes32 sessionId;
        address buyer;
        address seller;
        uint256 buyerPk;
        uint256 sellerPk;
        uint256 baseAmount;
        uint64 expiresAt;
        uint16 maxOffers;
        uint256 initialBase;
        uint256 initialQuote;
        // Ghost counters, advanced only when the chain accepted the action.
        uint64 consumedSequences;
        uint16 recordedOffers;
        uint256 settlements;
        uint256 settledQuoteAmount;
        bytes32 settledOfferHash;
        /// @dev The most recent digest this session displaced. Keeping it per session is what
        ///      makes invariant 6 falsifiable: a stale digest drawn from the global pool almost
        ///      always belonged to another session and was refused for the wrong reason.
        bytes32 lastReplaced;
        INegotiationExchange.Status terminalStatus;
    }

    NegotiationExchange public immutable exchange;
    MockERC20 public immutable baseToken;
    MockERC20 public immutable quoteToken;
    address public immutable operator;

    uint256 internal strangerPk;
    address internal stranger;

    Book[SLOTS] internal books;

    /// @notice Every offer digest the contract has ever accepted into `activeOfferHash`.
    bytes32[] public seenDigests;
    /// @notice Digests displaced by a later offer. Invariant 6: none of these may ever settle.
    mapping(bytes32 digest => bool) public wasReplaced;
    /// @notice Digests that actually settled a session.
    mapping(bytes32 digest => bool) public wasAccepted;

    /// @notice Signature forgeries the contract accepted. Invariant 7 requires zero.
    uint256 public forgeriesAccepted;
    /// @notice Calls that reverted, per action, for the invariant run summary.
    mapping(bytes32 action => uint256 count) public reverts;
    mapping(bytes32 action => uint256 count) public successes;

    constructor(
        NegotiationExchange exchange_,
        MockERC20 baseToken_,
        MockERC20 quoteToken_,
        address operator_
    ) {
        exchange = exchange_;
        baseToken = baseToken_;
        quoteToken = quoteToken_;
        operator = operator_;

        (stranger, strangerPk) = makeAddrAndKey("invariant-stranger");

        for (uint256 i = 0; i < SLOTS; i++) {
            Book storage book = books[i];
            (book.buyer, book.buyerPk) =
                makeAddrAndKey(string.concat("invariant-buyer-", vm.toString(i)));
            (book.seller, book.sellerPk) =
                makeAddrAndKey(string.concat("invariant-seller-", vm.toString(i)));
            book.sessionId = keccak256(abi.encodePacked("invariant-session", i));

            vm.startPrank(operator);
            baseToken.mint(book.seller, SELLER_BASE_BALANCE);
            quoteToken.mint(book.buyer, BUYER_QUOTE_BALANCE);
            vm.stopPrank();

            vm.prank(book.buyer);
            quoteToken.approve(address(exchange), type(uint256).max);
            vm.prank(book.seller);
            baseToken.approve(address(exchange), type(uint256).max);

            book.initialBase = SELLER_BASE_BALANCE;
            book.initialQuote = BUYER_QUOTE_BALANCE;

            if (i < PRE_OPENED) {
                _openSession(book, BASE_AMOUNT, SESSION_DURATION, _maxOffersFor(i));
                // Slot 2 has maxOffers 8, so it has room for the two offers that leave a displaced
                // digest behind. Slots 0 and 1 (limits 1 and 2) are left alone: their small limits
                // are what make the offer-limit boundary reachable.
                if (i == 2) _seedReplacedOffer(book);
            } else if (i == TERMINAL_SLOT) {
                _openSession(book, BASE_AMOUNT, SHORT_SESSION, DEFAULT_MAX_OFFERS);
                _seedClosedSession(book);
            }
        }
    }

    // ---------------------------------------------------------------------------------
    // Fuzzed actions
    // ---------------------------------------------------------------------------------

    /// @notice Open one of the slots the constructor left closed, with fuzzed terms.
    /// @dev `baseAmount` and `maxOffers` are left unbounded on purpose: zero and out-of-range
    ///      values must be refused by the contract, and `catch` is the assertion.
    function createSession(uint256 slotSeed, uint256 baseAmount, uint64 duration, uint16 maxOffers)
        external
    {
        Book storage book = books[_slot(slotSeed)];
        if (book.opened) {
            reverts["createSession"] += 1;
            return;
        }
        _openSession(book, baseAmount, duration, maxOffers);
    }

    /// @notice Record an offer at a fuzzed price and lifetime, usually from the side whose turn
    ///         it is and sometimes from the other.
    /// @dev `deviateSeed` decides which. A handler that chose the proposer by coin flip halved
    ///      the chance of every subsequent step being legal too, and a 32-call run then almost
    ///      never reached a settlement — so invariant 5's settled branch was never evaluated and
    ///      the suite passed on the strength of its unsettled branch alone. Deviating a quarter
    ///      of the time keeps `NotProposerTurn` covered from both directions while letting runs
    ///      get deep enough to trade.
    function recordOffer(
        uint256 slotSeed,
        uint256 quoteSeed,
        uint256 deviateSeed,
        uint64 lifetimeSeed
    ) external {
        Book storage book = books[_slot(slotSeed)];
        if (!book.opened) return;

        INegotiationExchange.SessionState memory state = exchange.getSession(book.sessionId);
        address proposer = _nextProposer(book, state);
        if (_deviates(deviateSeed)) proposer = _counterparty(book, proposer);

        uint64 lifetime = uint64(_bound(uint256(lifetimeSeed), 1, uint256(OFFER_LIFETIME)));
        uint64 validUntil = uint64(block.timestamp) + lifetime;
        if (validUntil > book.expiresAt) validUntil = book.expiresAt;

        INegotiationExchange.Offer memory offer = INegotiationExchange.Offer({
            sessionId: book.sessionId,
            configHash: _configHash(book),
            sequence: state.sequence + 1,
            proposer: proposer,
            quoteAmount: _bound(quoteSeed, 0, 200 * ONE_TOKEN),
            validUntil: validUntil
        });

        _submitOffer(book, offer, proposer, state.activeOfferHash);
    }

    /// @dev The recording body, shared by the fuzzed action and by the constructor's seeding, so
    ///      both keep the ghost state the same way. Ghost counters move only if the chain accepted.
    function _submitOffer(
        Book storage book_,
        INegotiationExchange.Offer memory offer,
        address proposer,
        bytes32 displaced
    ) private {
        bytes32 digest = exchange.hashOffer(offer);
        bytes memory signature = _sign(_keyOf(book_, proposer), digest);

        vm.prank(_relay());
        try exchange.recordOffer(offer, signature) {
            successes["recordOffer"] += 1;
            book_.consumedSequences += 1;
            book_.recordedOffers += 1;
            seenDigests.push(digest);
            if (displaced != bytes32(0) && displaced != digest) {
                wasReplaced[displaced] = true;
                book_.lastReplaced = displaced;
            }
        } catch {
            reverts["recordOffer"] += 1;
        }
    }

    /// @dev Record a buyer offer and then a seller counter, so the session starts the campaign with
    ///      one **displaced** digest already in hand.
    ///
    ///      Without this, invariant 6 was reachable only in runs where the fuzzer happened to put
    ///      two offers into the same session before anything terminated it — and the mutation that
    ///      deletes the `StaleOfferDigest` check was caught on some campaigns and not others.
    ///      A property whose falsification depends on the seed is not a property the suite
    ///      establishes. Seeding it makes `submitStaleAccept` have a target from call one.
    function _seedReplacedOffer(Book storage book_) private {
        uint64 validUntil = uint64(block.timestamp) + OFFER_LIFETIME;
        if (validUntil > book_.expiresAt) validUntil = book_.expiresAt;

        for (uint256 i = 0; i < 2; i++) {
            address proposer = i == 0 ? book_.buyer : book_.seller;
            INegotiationExchange.SessionState memory state = exchange.getSession(book_.sessionId);
            INegotiationExchange.Offer memory offer = INegotiationExchange.Offer({
                sessionId: book_.sessionId,
                configHash: _configHash(book_),
                sequence: state.sequence + 1,
                proposer: proposer,
                quoteAmount: (90 + i) * ONE_TOKEN,
                validUntil: validUntil
            });
            _submitOffer(book_, offer, proposer, state.activeOfferHash);
        }
        require(book_.lastReplaced != bytes32(0), "seeding a replaced digest failed");
    }

    /// @notice Accept the active offer, usually as the counterparty who is entitled to, and
    ///         sometimes as the proposer itself or against a digest already displaced.
    function acceptOffer(uint256 slotSeed, uint256 deviateSeed, uint256 digestSeed) external {
        Book storage book = books[_slot(slotSeed)];
        if (!book.opened) return;

        INegotiationExchange.SessionState memory state = exchange.getSession(book.sessionId);

        // Entitled actor: whoever did not propose the active offer.
        address actor = state.activeProposer == address(0)
            ? book.buyer
            : _counterparty(book, state.activeProposer);
        bytes32 offerHash = state.activeOfferHash;

        if (_deviates(deviateSeed)) {
            actor = _counterparty(book, actor); // self-acceptance
        }
        if (_deviates(digestSeed) && book.lastReplaced != bytes32(0)) {
            offerHash = book.lastReplaced; // A05: a digest this session has already displaced
        } else if (_deviates(digestSeed >> 8) && seenDigests.length > 0) {
            offerHash = seenDigests[_bound(digestSeed, 0, seenDigests.length - 1)];
        }

        INegotiationExchange.Accept memory acceptance = INegotiationExchange.Accept({
            sessionId: book.sessionId,
            configHash: _configHash(book),
            sequence: state.sequence + 1,
            actor: actor,
            offerHash: offerHash
        });
        bytes memory signature = _sign(_keyOf(book, actor), exchange.hashAccept(acceptance));

        vm.prank(_relay());
        try exchange.acceptAndSettle(acceptance, signature) {
            successes["acceptOffer"] += 1;
            book.consumedSequences += 1;
            book.settlements += 1;
            book.settledQuoteAmount = state.activeQuoteAmount;
            book.settledOfferHash = offerHash;
            _recordTerminal(book, INegotiationExchange.Status.Settled);
            wasAccepted[offerHash] = true;
        } catch {
            reverts["acceptOffer"] += 1;
        }
    }

    /// @notice Accept a digest this session has already displaced, correctly signed by the party
    ///         entitled to accept, at the right sequence, while the session is still open.
    /// @dev A05 as a property, and a dedicated action rather than a branch of `acceptOffer`,
    ///      because as a branch it was unreachable in practice: the valid acceptance is drawn
    ///      three times as often and settles the session first, so deleting the
    ///      `StaleOfferDigest` check survived mutation. Everything about this call is legal
    ///      except the one thing under test, which is what makes a success here meaningful —
    ///      the contract has no other reason to refuse it (docs/protocol.md section 5 rule 5).
    function submitStaleAccept(uint256 slotSeed) external {
        Book storage book = books[_slot(slotSeed)];
        if (!book.opened || book.lastReplaced == bytes32(0)) return;

        INegotiationExchange.SessionState memory state = exchange.getSession(book.sessionId);
        address actor = state.activeProposer == address(0)
            ? book.buyer
            : _counterparty(book, state.activeProposer);

        INegotiationExchange.Accept memory acceptance = INegotiationExchange.Accept({
            sessionId: book.sessionId,
            configHash: _configHash(book),
            sequence: state.sequence + 1,
            actor: actor,
            offerHash: book.lastReplaced
        });
        bytes memory signature = _sign(_keyOf(book, actor), exchange.hashAccept(acceptance));

        vm.prank(_relay());
        try exchange.acceptAndSettle(acceptance, signature) {
            // Recorded, not asserted here: invariant 6 is the assertion.
            book.consumedSequences += 1;
            book.settlements += 1;
            book.settledQuoteAmount = state.activeQuoteAmount;
            book.settledOfferHash = book.lastReplaced;
            _recordTerminal(book, INegotiationExchange.Status.Settled);
            wasAccepted[book.lastReplaced] = true;
        } catch {
            reverts["submitStaleAccept"] += 1;
        }
    }

    /// @notice Walk away, from either side, with a fuzzed reason code.
    function closeSession(uint256 slotSeed, bool fromBuyer, uint8 reason) external {
        Book storage book = books[_slot(slotSeed)];
        if (!book.opened) return;

        INegotiationExchange.Close memory closure = INegotiationExchange.Close({
            sessionId: book.sessionId,
            configHash: _configHash(book),
            sequence: _nextSequence(book),
            actor: fromBuyer ? book.buyer : book.seller,
            reason: _deviates(uint256(reason)) ? reason : uint8(_bound(uint256(reason), 1, 3))
        });
        bytes memory signature =
            _sign(fromBuyer ? book.buyerPk : book.sellerPk, exchange.hashClose(closure));

        vm.prank(_relay());
        try exchange.closeSession(closure, signature) {
            successes["closeSession"] += 1;
            book.consumedSequences += 1;
            _recordTerminal(book, INegotiationExchange.Status.Closed);
        } catch {
            reverts["closeSession"] += 1;
        }
    }

    function expireSession(uint256 slotSeed) external {
        Book storage book = books[_slot(slotSeed)];
        if (!book.opened) return;

        vm.prank(_relay());
        try exchange.expireSession(book.sessionId) {
            successes["expireSession"] += 1;
            _recordTerminal(book, INegotiationExchange.Status.Expired);
        } catch {
            reverts["expireSession"] += 1;
        }
    }

    /// @dev `callerSeed` sometimes makes the caller a non-operator, so `NotOperator` is covered.
    function abortSession(uint256 slotSeed, uint8 reason, bool asOperator) external {
        Book storage book = books[_slot(slotSeed)];
        if (!book.opened) return;

        uint8 code = _deviates(uint256(reason)) ? reason : uint8(_bound(uint256(reason), 1, 4));

        vm.prank(asOperator ? operator : stranger);
        try exchange.abortSession(book.sessionId, code) {
            successes["abortSession"] += 1;
            _recordTerminal(book, INegotiationExchange.Status.Aborted);
        } catch {
            reverts["abortSession"] += 1;
        }
    }

    /// @notice Present a well-formed message with a broken signature.
    /// @dev Invariant 7. Six mutation kinds: a flipped bit in `r`, in `s`, a swapped `v`, the
    ///      wrong key, the counterparty's key, and a signature over a forged domain separator.
    ///      Any acceptance increments `forgeriesAccepted`, which the invariant requires to stay
    ///      at zero — a revert here is the expected outcome and is not counted as a failure.
    function submitForgedOffer(uint256 slotSeed, uint8 mutationSeed, uint256 quoteSeed) external {
        Book storage book = books[_slot(slotSeed)];
        if (!book.opened) return;

        uint64 validUntil = uint64(block.timestamp) + OFFER_LIFETIME;
        if (validUntil > book.expiresAt) validUntil = book.expiresAt;

        INegotiationExchange.Offer memory offer = INegotiationExchange.Offer({
            sessionId: book.sessionId,
            configHash: _configHash(book),
            sequence: _nextSequence(book),
            proposer: book.buyer,
            quoteAmount: _bound(quoteSeed, 1, 200 * ONE_TOKEN),
            validUntil: validUntil
        });

        bytes memory signature = _forge(
            uint8(_bound(uint256(mutationSeed), 0, 5)),
            book.buyerPk,
            book.sellerPk,
            exchange.hashOffer(offer)
        );

        vm.prank(_relay());
        try exchange.recordOffer(offer, signature) {
            forgeriesAccepted += 1;
        } catch {
            reverts["submitForgedOffer"] += 1;
        }
    }

    /// @notice As `submitForgedOffer`, against the only entry point that moves tokens.
    function submitForgedAccept(uint256 slotSeed, uint8 mutationSeed) external {
        Book storage book = books[_slot(slotSeed)];
        if (!book.opened) return;

        INegotiationExchange.Accept memory acceptance = INegotiationExchange.Accept({
            sessionId: book.sessionId,
            configHash: _configHash(book),
            sequence: _nextSequence(book),
            actor: book.seller,
            offerHash: exchange.getSession(book.sessionId).activeOfferHash
        });

        bytes memory signature = _forge(
            uint8(_bound(uint256(mutationSeed), 0, 5)),
            book.sellerPk,
            book.buyerPk,
            exchange.hashAccept(acceptance)
        );

        vm.prank(_relay());
        try exchange.acceptAndSettle(acceptance, signature) {
            forgeriesAccepted += 1;
        } catch {
            reverts["submitForgedAccept"] += 1;
        }
    }

    /// @notice Move chain time forward, so offer lifetimes and session deadlines both elapse.
    /// @dev Bounded at 600 s a call against a 1,800 s session: with the configured depth, a run
    ///      reaches expiry well before its last call, and `expireSession` becomes the only legal
    ///      action (docs/protocol.md section 6).
    function advanceTime(uint256 secondsSeed) external {
        vm.warp(block.timestamp + _bound(secondsSeed, 1, 600));
        successes["advanceTime"] += 1;
    }

    // ---------------------------------------------------------------------------------
    // Views for the invariant contract
    // ---------------------------------------------------------------------------------

    function slots() external pure returns (uint256) {
        return SLOTS;
    }

    function bookAt(uint256 index) external view returns (Book memory) {
        return books[index];
    }

    function seenDigestCount() external view returns (uint256) {
        return seenDigests.length;
    }

    // ---------------------------------------------------------------------------------
    // Internals
    // ---------------------------------------------------------------------------------

    function _openSession(Book storage book_, uint256 baseAmount, uint64 duration, uint16 maxOffers)
        private
    {
        INegotiationExchange.SessionConfig memory config = INegotiationExchange.SessionConfig({
            sessionId: book_.sessionId,
            buyer: book_.buyer,
            seller: book_.seller,
            baseAmount: baseAmount,
            expiresAt: uint64(block.timestamp) + duration,
            maxOffers: maxOffers
        });

        vm.prank(operator);
        try exchange.createSession(config) {
            successes["createSession"] += 1;
            book_.opened = true;
            book_.baseAmount = config.baseAmount;
            book_.expiresAt = config.expiresAt;
            book_.maxOffers = config.maxOffers;

            // The handler's `_configHash` is a fifth restatement of the section 3 encoding, and
            // until this line nothing checked it. If it drifted from the contract's, every action
            // the handler built would revert with `ConfigHashMismatch`, the `try`/`catch` would
            // swallow it, and all seven invariants would pass over empty ghost state — a green
            // suite testing nothing. Verified by mutation: swap two arguments in `_configHash` and
            // this fires in `setUp`, deterministically, rather than depending on the seed.
            assertEq(
                exchange.getSession(book_.sessionId).configHash,
                _configHash(book_),
                "the handler's configHash disagrees with the contract's"
            );
        } catch {
            reverts["createSession"] += 1;
        }
    }

    /// @dev docs/protocol.md section 3, recomputed rather than read back from the contract, so
    ///      a change to the encoding shows up here as `ConfigHashMismatch` on every action.
    function _configHash(Book storage book_) private view returns (bytes32) {
        return keccak256(
            abi.encode(
                book_.sessionId,
                book_.buyer,
                book_.seller,
                address(baseToken),
                address(quoteToken),
                book_.baseAmount,
                book_.expiresAt,
                book_.maxOffers
            )
        );
    }

    /// @dev Read from the chain rather than from the ghost counter: an action whose sequence came
    ///      from the ghost record would be self-consistent even if the contract had stopped
    ///      storing sequences at all.
    function _nextSequence(Book storage book_) private view returns (uint64) {
        return exchange.getSession(book_.sessionId).sequence + 1;
    }

    /// @dev Close the session at once, so a terminal session exists before the first fuzzed call.
    ///      Paired with `SHORT_SESSION`, it is also past its deadline after one `advanceTime`,
    ///      which is the state invariant 1 has to be able to fail in.
    function _seedClosedSession(Book storage book_) private {
        INegotiationExchange.SessionState memory state = exchange.getSession(book_.sessionId);
        INegotiationExchange.Close memory closure = INegotiationExchange.Close({
            sessionId: book_.sessionId,
            configHash: _configHash(book_),
            sequence: state.sequence + 1,
            actor: book_.seller,
            reason: 1 // terms_unacceptable
        });
        bytes memory signature = _sign(book_.sellerPk, exchange.hashClose(closure));

        vm.prank(_relay());
        exchange.closeSession(closure, signature);

        book_.consumedSequences += 1;
        _recordTerminal(book_, INegotiationExchange.Status.Closed);
    }

    /// @dev The ghost record of how a session ended is written once. Following the chain instead
    ///      is what let a mutation past: with the status check removed from `expireSession`, an
    ///      already-settled session could be re-marked Expired, the handler dutifully recorded
    ///      Expired, and invariant 1 compared the chain against itself and passed. The first
    ///      terminal status the chain accepted is the one the invariant holds it to.
    function _recordTerminal(Book storage book_, INegotiationExchange.Status status) private {
        if (book_.terminalStatus == INegotiationExchange.Status.None) {
            book_.terminalStatus = status;
        }
    }

    /// @dev docs/protocol.md rule 5.3, computed off-chain so a wrong-turn offer can be built
    ///      deliberately. `activeProposer` still names the last proposer after an expired offer,
    ///      which is the case ADR-016 settles.
    function _nextProposer(Book storage book_, INegotiationExchange.SessionState memory state)
        private
        view
        returns (address)
    {
        if (state.offerCount == 0) return book_.buyer;
        return _counterparty(book_, state.activeProposer);
    }

    function _counterparty(Book storage book_, address party) private view returns (address) {
        return party == book_.buyer ? book_.seller : book_.buyer;
    }

    function _keyOf(Book storage book_, address party) private view returns (uint256) {
        return party == book_.buyer ? book_.buyerPk : book_.sellerPk;
    }

    /// @dev True for roughly a quarter of seeds: often enough that every illegal variant is
    ///      reached within a campaign, rarely enough that runs still get deep.
    function _deviates(uint256 seed) private pure returns (bool) {
        return _bound(seed, 0, 99) < 25;
    }

    function _sign(uint256 privateKey, bytes32 digest) private pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    /// @dev Six ways for a signature to be wrong, all relative to `claimedPk` — the key of the
    ///      party the message actually names. Passing that key in rather than assuming a role is
    ///      the whole point: the first version of this helper always mutated the buyer's
    ///      signature, so for an acceptance the *seller* names itself and signs, kind 4 handed
    ///      back a perfectly valid signature, and the invariant correctly reported that a
    ///      "forgery" had settled a session.
    ///
    ///      Which error the contract raises is a unit-test concern (`Roles.t.sol`,
    ///      `Domain.t.sol`); here the only question is whether any of them is accepted.
    function _forge(uint8 kind, uint256 claimedPk, uint256 counterpartyPk, bytes32 digest)
        private
        view
        returns (bytes memory)
    {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(claimedPk, digest);

        if (kind == 0) return abi.encodePacked(r ^ bytes32(uint256(1)), s, v);
        if (kind == 1) return abi.encodePacked(r, s ^ bytes32(uint256(1)), v);
        if (kind == 2) return abi.encodePacked(r, s, v == 27 ? uint8(28) : uint8(27));
        if (kind == 3) return _sign(strangerPk, digest);
        if (kind == 4) return _sign(counterpartyPk, digest);

        // A signature over the same struct hash under a domain separator for another chain and
        // another contract: correct key, correct fields, wrong domain (A07).
        bytes32 forgedSeparator = keccak256(
            abi.encode(
                keccak256(
                    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                ),
                keccak256("AgentNegotiationSandbox"),
                keccak256("1"),
                block.chainid + 1,
                address(this)
            )
        );
        return _sign(claimedPk, keccak256(abi.encodePacked(hex"1901", forgedSeparator, digest)));
    }

    function _slot(uint256 seed) private pure returns (uint256) {
        return _bound(seed, 0, SLOTS - 1);
    }

    /// @dev A gas payer with no signing authority, as in production (docs/protocol.md section 1).
    function _relay() private pure returns (address) {
        return address(uint160(uint256(keccak256("invariant-relay"))));
    }
}
