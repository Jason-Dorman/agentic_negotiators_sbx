/**
 * Reason codes, as the two tables of docs/protocol.md section 10.
 *
 * The TypeScript half of a table that also exists in Python
 * (`src/negotiation_protocol/reasons.py`). Both are checked against
 * `fixtures/reason_codes.v1.json`, so a code added on one side alone fails the build rather than
 * producing a timeline sentence that describes a different walk-away than the chain recorded.
 *
 * A close reason is the agent's *statement*, not a verified fact: `inventory_constraint` means the
 * agent said it was at its inventory floor, and the validator checked that claim separately.
 */

export type CloseReason = 'terms_unacceptable' | 'inventory_constraint' | 'no_further_concession';

export type AbortReason =
  'operator_request' | 'model_failure' | 'budget_exhausted' | 'execution_failure';

/** Participant walk-away codes, signed in `Close.reason`. */
export const CLOSE_REASONS: Readonly<Record<number, CloseReason>> = Object.freeze({
  1: 'terms_unacceptable',
  2: 'inventory_constraint',
  3: 'no_further_concession',
});

/** Operator abort codes, passed to `abortSession`. Never signed by a participant. */
export const ABORT_REASONS: Readonly<Record<number, AbortReason>> = Object.freeze({
  1: 'operator_request',
  2: 'model_failure',
  3: 'budget_exhausted',
  4: 'execution_failure',
});

export const CLOSE_REASON_CODES: Readonly<Record<CloseReason, number>> = Object.freeze({
  terms_unacceptable: 1,
  inventory_constraint: 2,
  no_further_concession: 3,
});

export const ABORT_REASON_CODES: Readonly<Record<AbortReason, number>> = Object.freeze({
  operator_request: 1,
  model_failure: 2,
  budget_exhausted: 3,
  execution_failure: 4,
});

/**
 * A code outside the protocol's tables.
 *
 * Thrown rather than defaulted. An unrecognised code means this package and the deployed contract
 * disagree, and rendering it as "unknown" in a timeline would present a protocol mismatch as an
 * ordinary outcome.
 */
export class UnknownReasonCodeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UnknownReasonCodeError';
  }
}

export function closeReasonName(code: number): CloseReason {
  const name = CLOSE_REASONS[code];
  if (name === undefined) {
    throw new UnknownReasonCodeError(`close reason ${String(code)} is not one of 1, 2, 3`);
  }
  return name;
}

export function abortReasonName(code: number): AbortReason {
  const name = ABORT_REASONS[code];
  if (name === undefined) {
    throw new UnknownReasonCodeError(`abort reason ${String(code)} is not one of 1, 2, 3, 4`);
  }
  return name;
}
