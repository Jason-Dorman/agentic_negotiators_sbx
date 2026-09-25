// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {Test, console} from "forge-std/Test.sol";

import {ExchangeHandler} from "./ExchangeHandler.sol";
import {MockERC20} from "../../src/MockERC20.sol";
import {NegotiationExchange} from "../../src/NegotiationExchange.sol";
import {INegotiationExchange} from "../../src/interfaces/INegotiationExchange.sol";

/// @notice The seven properties of docs/test_strategy.md section 4.2, checked after every call
///         the fuzzer makes.
/// @dev These are the statements the rest of the system is allowed to assume. The unit suite
///      proves that each specific rule produces its specific error; this file proves that no
///      *sequence* of legal and illegal actions can leave the contract in a state the
///      application would misread as an economic outcome.
///
///      Every invariant is written against ghost state the handler advances only on success,
///      and against the tokens' own balances — never against a value read back from the same
///      storage slot the invariant is about.
///
///      `fail_on_revert = false` in `foundry.toml`: most calls the fuzzer makes are illegal by
///      construction and reverting is the correct outcome. What must never happen is a call that
///      succeeds when it should not, and each invariant below is an assertion about that.
contract NegotiationExchangeInvariantTest is Test {
    address internal operator = makeAddr("invariant-operator");

    MockERC20 internal baseToken;
    MockERC20 internal quoteToken;
    NegotiationExchange internal exchange;
    ExchangeHandler internal handler;

    function setUp() public {
        vm.warp(1_760_000_000);

        vm.startPrank(operator);
        baseToken = new MockERC20("Mock Asset", "mASSET", operator);
        quoteToken = new MockERC20("Mock USD", "mUSD", operator);
        exchange = new NegotiationExchange(address(baseToken), address(quoteToken), operator);
        vm.stopPrank();

        handler = new ExchangeHandler(exchange, baseToken, quoteToken, operator);

        targetContract(address(handler));

        bytes4[] memory selectors = new bytes4[](10);
        selectors[0] = ExchangeHandler.createSession.selector;
        selectors[1] = ExchangeHandler.recordOffer.selector;
        selectors[2] = ExchangeHandler.acceptOffer.selector;
        selectors[3] = ExchangeHandler.closeSession.selector;
        selectors[4] = ExchangeHandler.expireSession.selector;
        selectors[5] = ExchangeHandler.abortSession.selector;
        selectors[6] = ExchangeHandler.submitForgedOffer.selector;
        selectors[7] = ExchangeHandler.submitForgedAccept.selector;
        selectors[8] = ExchangeHandler.submitStaleAccept.selector;
        selectors[9] = ExchangeHandler.advanceTime.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: selectors}));

        // The handler is the only actor. Without this, the fuzzer would also call the tokens
        // and the exchange directly as itself, which exercises nothing the unit suite misses
        // and drowns the interesting sequences in `NotOperator` reverts.
        excludeContract(address(baseToken));
        excludeContract(address(quoteToken));
        excludeContract(address(exchange));
    }

    /// @notice Invariant 1: status is monotonic — once non-Open, it never changes again.
    /// @dev The handler records the terminal status it saw the chain accept. If a later action
    ///      moved a settled session to closed, or reopened an expired one, the two disagree.
    ///      This is the property `outcome_kind` in the database depends on: the economic result
    ///      of a run is decided once (docs/data_model.md section 5).
    function invariant_status_is_terminal_once_reached() public view {
        for (uint256 i = 0; i < handler.slots(); i++) {
            ExchangeHandler.Book memory book = handler.bookAt(i);
            if (!book.opened || book.terminalStatus == INegotiationExchange.Status.None) continue;

            assertEq(
                uint8(exchange.getSession(book.sessionId).status),
                uint8(book.terminalStatus),
                "terminal status changed after the fact"
            );
        }
    }

    /// @notice Invariant 2: `sequence` advances by exactly one per successful signed action.
    /// @dev Stated as an equality rather than as monotonicity, which is stronger and cheaper to
    ///      check: sequence starts at 0 and every accepted Offer, Accept or Close consumes
    ///      exactly one, so the stored value must equal the count of accepted actions. A
    ///      double-increment, a skipped increment or an increment on a lifecycle action all
    ///      break it (docs/protocol.md section 5).
    function invariant_sequence_equals_accepted_action_count() public view {
        for (uint256 i = 0; i < handler.slots(); i++) {
            ExchangeHandler.Book memory book = handler.bookAt(i);
            if (!book.opened) continue;

            assertEq(
                exchange.getSession(book.sessionId).sequence,
                book.consumedSequences,
                "sequence does not match the number of accepted signed actions"
            );
        }
    }

    /// @notice Invariant 3: `offerCount` never exceeds `maxOffers`, and counts what was recorded.
    function invariant_offer_count_is_bounded_and_accurate() public view {
        for (uint256 i = 0; i < handler.slots(); i++) {
            ExchangeHandler.Book memory book = handler.bookAt(i);
            if (!book.opened) continue;

            INegotiationExchange.SessionState memory state = exchange.getSession(book.sessionId);
            assertEq(
                state.offerCount, book.recordedOffers, "offerCount drifted from the ghost count"
            );
            assertLe(state.offerCount, book.maxOffers, "offerCount exceeded maxOffers");
        }
    }

    /// @notice Invariant 4: tokens are conserved, and the exchange never holds a balance.
    /// @dev Settlement is a transfer between the two participants, never a mint, a burn or a
    ///      deposit (docs/protocol.md section 2). Each session has its own wallet pair, so the
    ///      per-session sum is the whole of that session's economy.
    function invariant_tokens_are_conserved_and_the_exchange_holds_nothing() public view {
        for (uint256 i = 0; i < handler.slots(); i++) {
            ExchangeHandler.Book memory book = handler.bookAt(i);
            if (book.buyer == address(0)) continue;

            assertEq(
                baseToken.balanceOf(book.buyer) + baseToken.balanceOf(book.seller),
                book.initialBase,
                "base token was created or destroyed"
            );
            assertEq(
                quoteToken.balanceOf(book.buyer) + quoteToken.balanceOf(book.seller),
                book.initialQuote,
                "quote token was created or destroyed"
            );
        }

        assertEq(baseToken.balanceOf(address(exchange)), 0, "exchange holds base token");
        assertEq(quoteToken.balanceOf(address(exchange)), 0, "exchange holds quote token");
    }

    /// @notice Invariant 5: a settled session moved exactly the signed amounts, exactly once;
    ///         an unsettled session moved nothing.
    /// @dev The second half is the one that matters most. A contract that transferred tokens on
    ///      a *failed* acceptance would satisfy every other invariant here — status still Open,
    ///      sequence unchanged, tokens conserved — and would still have executed a trade nobody
    ///      agreed to.
    function invariant_settlement_moved_exactly_the_signed_amounts() public view {
        for (uint256 i = 0; i < handler.slots(); i++) {
            ExchangeHandler.Book memory book = handler.bookAt(i);
            if (!book.opened) continue;

            INegotiationExchange.SessionState memory state = exchange.getSession(book.sessionId);

            if (state.status == INegotiationExchange.Status.Settled) {
                assertEq(book.settlements, 1, "a session settled more than once");
                assertEq(baseToken.balanceOf(book.buyer), book.baseAmount, "wrong base leg");
                assertEq(
                    quoteToken.balanceOf(book.seller), book.settledQuoteAmount, "wrong quote leg"
                );
                assertEq(
                    baseToken.balanceOf(book.seller),
                    book.initialBase - book.baseAmount,
                    "base debit"
                );
                assertEq(
                    quoteToken.balanceOf(book.buyer),
                    book.initialQuote - book.settledQuoteAmount,
                    "quote debit"
                );
                assertEq(state.activeOfferHash, bytes32(0), "settled session kept an active offer");
            } else {
                assertEq(book.settlements, 0, "an unsettled session recorded a settlement");
                assertEq(baseToken.balanceOf(book.buyer), 0, "base moved without a settlement");
                assertEq(quoteToken.balanceOf(book.seller), 0, "quote moved without a settlement");
            }
        }
    }

    /// @notice Invariant 6: a digest displaced by a later offer never settles anything.
    /// @dev A05 as a property rather than as a single case. The unit test proves one replaced
    ///      digest is refused; this proves no reachable interleaving of offers and acceptances
    ///      lets one through (docs/protocol.md section 5 rule 5).
    function invariant_a_replaced_digest_never_settles() public view {
        for (uint256 i = 0; i < handler.seenDigestCount(); i++) {
            bytes32 digest = handler.seenDigests(i);
            assertFalse(
                handler.wasReplaced(digest) && handler.wasAccepted(digest),
                "a replaced offer digest was accepted"
            );
        }
    }

    /// @notice Invariant 7: no mutated, mis-keyed or wrong-domain signature is ever accepted.
    /// @dev The handler submits six kinds of forgery. Signature verification is the whole of
    ///      the authority boundary: the relay pays for gas and cannot trade, and that is true
    ///      only for as long as this counter stays at zero (docs/protocol.md section 1).
    function invariant_no_forged_signature_is_accepted() public view {
        assertEq(handler.forgeriesAccepted(), 0, "a forged signature was accepted");
    }

    /// @dev Printed with `-vv`, and it reports **one run**, not the campaign: Foundry calls this
    ///      once, after the last run of the 64, and the handler's counters reset between runs. So
    ///      read it as a spot check that the handler's actions are landing at all, not as a measure
    ///      of coverage — a run showing `acceptOffer ok 0` does not mean the campaign never
    ///      settled.
    ///
    ///      Not an assertion either: a run in which nothing settled is a legitimate random walk,
    ///      and asserting liveness here would make the suite flaky. Non-vacuity is established by
    ///      mutation instead (docs/contributing.md section 3, docs/test_strategy.md section 4.2),
    ///      which is a claim about the whole campaign rather than about one walk through it.
    function afterInvariant() public view {
        console.log(
            "createSession  ok/revert",
            handler.successes("createSession"),
            handler.reverts("createSession")
        );
        console.log(
            "recordOffer    ok/revert",
            handler.successes("recordOffer"),
            handler.reverts("recordOffer")
        );
        console.log(
            "acceptOffer    ok/revert",
            handler.successes("acceptOffer"),
            handler.reverts("acceptOffer")
        );
        console.log(
            "closeSession   ok/revert",
            handler.successes("closeSession"),
            handler.reverts("closeSession")
        );
        console.log(
            "expireSession  ok/revert",
            handler.successes("expireSession"),
            handler.reverts("expireSession")
        );
        console.log(
            "abortSession   ok/revert",
            handler.successes("abortSession"),
            handler.reverts("abortSession")
        );
        console.log("stale accept   refused  ", handler.reverts("submitStaleAccept"));
        console.log("forged offer   refused  ", handler.reverts("submitForgedOffer"));
        console.log("forged accept  refused  ", handler.reverts("submitForgedAccept"));
    }
}
