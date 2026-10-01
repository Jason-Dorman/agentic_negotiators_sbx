"""Does the observation agree with itself and with the session this instance approved? (ADR-046)

The observation is built by our own backend from the chain, and the agent cannot read the chain. So
the agent cannot tell a true observation from a false one, but it can tell a self-contradictory one:
`expected_sequence` that is not the history's next sequence, an `active_offer` that is not the last
recorded offer, an offer digest that does not hash from the offer's own fields. Each of those is a
backend bug or a stale read, and signing on it would produce an action the contract reverts — an
execution failure and an abort for what was a bookkeeping error.

So the agent refuses with `422 observation_inconsistent`, naming every contradiction, and signs
nothing. The controller rebuilds the observation from the chain and retries up to five times, then
moves the run to `RECOVERY_REQUIRED` (stage 2.4).

What is checked, against docs/protocol.md sections 5 and 12:

- history is the session's recorded offers in ascending sequence, `1, 2, …`, alternating from the
  buyer (rule 3), no more than `maxOffers` of them, each with all of its fields, the last one
  `active` while unexpired and `expired` after, every earlier one `replaced`;
- each offer's digest is the EIP-712 digest of its own fields under the approved session, proposer
  being that role's address — which binds the hash a policy may accept to the amount the mandate
  checks were run against;
- `expected_sequence` is the next sequence; `offers_remaining_for_me` is this party's share less the
  offers it has recorded;
- `active_offer` is the last offer while it is unexpired, and null otherwise: an expired offer no
  longer stands (section 12).

Messages name the location and the rule, never a value.
"""

from __future__ import annotations

from agent.observation import HistoryEntry, Observation, as_role
from agent.policy import opportunities
from agent.signing import ApprovedSession
from negotiation_protocol import UINT64_MAX, Digest, Offer


def contradictions(observation: Observation, approval: ApprovedSession) -> dict[str, str]:
    problems: dict[str, str] = {}
    history = observation.history
    for index, entry in enumerate(history):
        problems.update(_entry_problems(index, entry, len(history), observation, approval))
    if len(history) > observation.session.max_offers:
        problems["history"] = "holds more offers than the session's maxOffers"
    if observation.expected_sequence != len(history) + 1:
        problems["expected_sequence"] = "is not the sequence after the last history entry"
    own = observation.own_offers_recorded()
    share = opportunities(observation.role, observation.session.max_offers)
    if observation.offers_remaining_for_me != max(share - own, 0):
        problems["offers_remaining_for_me"] = "is not this party's share less its recorded offers"
    problems.update(_active_offer_problems(observation))
    return problems


def _entry_problems(
    index: int,
    entry: HistoryEntry,
    count: int,
    observation: Observation,
    approval: ApprovedSession,
) -> dict[str, str]:
    where = f"history/{index}"
    if entry.sequence != index + 1:
        return {f"{where}/sequence": "history must be in ascending sequence order from 1"}
    if entry.kind != "offer":
        return {f"{where}/kind": "only offers can precede a turn; any other action is terminal"}
    expected_actor = "buyer" if index % 2 == 0 else "seller"
    if entry.actor != expected_actor:
        return {f"{where}/actor": "offers must alternate, beginning with the buyer"}
    quote, valid_until, offer_hash = entry.quote_amount, entry.valid_until, entry.offer_hash
    if quote is None or valid_until is None or offer_hash is None or entry.status is None:
        return {where: "an offer must carry quote_amount_minor, valid_until, offer_hash and status"}
    problems: dict[str, str] = {}
    actor = approval.party(as_role(entry.actor))
    if not _digest_matches(
        approval, int(entry.sequence), actor, int(quote), valid_until, offer_hash
    ):
        problems[f"{where}/offer_hash"] = "is not the EIP-712 digest of this offer's own fields"
    last = index == count - 1
    expected = (
        ("active" if valid_until > observation.chain_time else "expired") if last else "replaced"
    )
    if entry.status != expected:
        problems[f"{where}/status"] = (
            "the last offer is active until valid_until and expired after; earlier ones replaced"
        )
    return problems


def _digest_matches(
    approval: ApprovedSession,
    sequence: int,
    proposer: str,
    quote: int,
    valid_until: int,
    offer_hash: Digest,
) -> bool:
    if not 0 <= valid_until <= UINT64_MAX:
        return False
    offer = Offer(
        approval.session_id.to_bytes(),
        approval.config_hash.to_bytes(),
        sequence,
        proposer,
        quote,
        valid_until,
    )
    return Digest(offer.digest(approval.domain())) == offer_hash


def _active_offer_problems(observation: Observation) -> dict[str, str]:
    history, active = observation.history, observation.active_offer
    last = history[-1] if history else None
    standing = (
        last is not None
        and last.valid_until is not None
        and (last.valid_until > observation.chain_time)
    )
    if active is None:
        if standing:
            return {"active_offer": "is null while the last recorded offer is unexpired"}
        return {}
    if not standing or last is None:
        return {"active_offer": "is set, but no unexpired recorded offer stands"}
    same = (
        active.offer_hash == last.offer_hash
        and active.proposer == last.actor
        and active.quote_amount == last.quote_amount
        and active.valid_until == last.valid_until
        and active.sequence == last.sequence
    )
    return {} if same else {"active_offer": "is not the last recorded offer"}
