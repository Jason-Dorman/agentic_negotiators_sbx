/**
 * Shared protocol definitions: schemas, ABI artefacts, fixtures and reason tables.
 *
 * The normative source is docs/protocol.md; this package is its machine-readable form and nothing
 * more. It imports nothing from services or apps (docs/contributing.md section 1).
 *
 * Schemas, fixtures and ABI artefacts are reached through the package's `exports` map — for
 * example `@negotiation/protocol/schemas/observation.v1.json` — rather than re-exported here, so
 * that a consumer takes only the file it needs and the same paths work from Python and Solidity.
 */

export {
  ABORT_REASON_CODES,
  ABORT_REASONS,
  CLOSE_REASON_CODES,
  CLOSE_REASONS,
  UnknownReasonCodeError,
  abortReasonName,
  closeReasonName,
} from './reasons.js';
export type { AbortReason, CloseReason } from './reasons.js';

export { ACCEPT_TYPES, CLOSE_TYPES, EIP712_DOMAIN_TYPES, OFFER_TYPES, domain } from './eip712.js';
export type { Accept, Close, NegotiationDomain, Offer } from './eip712.js';

/** docs/protocol.md section 15. Frozen at release v0.1. */
export const PROTOCOL_VERSION = 1 as const;

/** The EIP-712 domain `version`, which moves with the protocol version (section 15). */
export const DOMAIN_VERSION = '1' as const;
