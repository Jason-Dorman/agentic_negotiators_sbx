/**
 * The EIP-712 domain and the three typed-message definitions, in viem's shape.
 *
 * docs/protocol.md section 4. These structures are the TypeScript statement of the same type
 * strings the contract hashes and the Python generator writes; `tests/eip712.test.ts` checks that
 * viem's digest over them equals the fixture's, which is what makes them a statement rather than a
 * guess.
 *
 * The web client never signs anything (docs/contributing.md section 2.2): it holds no key and has
 * no signing path. These definitions exist so the interface can *verify* — recompute a digest from
 * an export and recover the signer — which is how a viewer confirms the authority chain without
 * trusting the backend that served it.
 */

import type { Address, Hex } from 'viem';

/**
 * The domain, fully determined — every field required and the two constants literal types.
 *
 * Narrower than viem's `TypedDataDomain`, whose fields are all optional. That matters: a domain
 * with an absent `chainId` still type-checks as a `TypedDataDomain` and still hashes, to a
 * different separator, and every signature built from it would be rejected on-chain with nothing
 * to say why (docs/protocol.md section 4).
 */
export interface NegotiationDomain {
  readonly name: 'AgentNegotiationSandbox';
  readonly version: '1';
  readonly chainId: bigint;
  readonly verifyingContract: Address;
}

/**
 * docs/protocol.md section 4. Both arguments come from the deployment manifest, never from a
 * user-supplied value: the domain is what binds a signature to one chain and one contract.
 *
 * `chainId` is a `bigint` because the type string declares it `uint256`. Accepting a `number` at
 * the boundary and widening here keeps callers reading a manifest's JSON integer from having to
 * convert (docs/contributing.md section 2.2).
 */
export function domain(chainId: bigint | number, verifyingContract: Address): NegotiationDomain {
  return {
    name: 'AgentNegotiationSandbox',
    version: '1',
    chainId: BigInt(chainId),
    verifyingContract,
  };
}

/**
 * The domain type itself. Exported because viem's `hashDomain` needs it passed in, and because a
 * viewer verifying an export computes the separator before any message digest.
 */
export const EIP712_DOMAIN_TYPES = {
  EIP712Domain: [
    { name: 'name', type: 'string' },
    { name: 'version', type: 'string' },
    { name: 'chainId', type: 'uint256' },
    { name: 'verifyingContract', type: 'address' },
  ],
} as const;

/**
 * Field order matters: it is the order in the type string, and the type string is keccak-hashed
 * into the type hash. Reordering these is a different protocol.
 */
export const OFFER_TYPES = {
  Offer: [
    { name: 'sessionId', type: 'bytes32' },
    { name: 'configHash', type: 'bytes32' },
    { name: 'sequence', type: 'uint64' },
    { name: 'proposer', type: 'address' },
    { name: 'quoteAmount', type: 'uint256' },
    { name: 'validUntil', type: 'uint64' },
  ],
} as const;

export const ACCEPT_TYPES = {
  Accept: [
    { name: 'sessionId', type: 'bytes32' },
    { name: 'configHash', type: 'bytes32' },
    { name: 'sequence', type: 'uint64' },
    { name: 'actor', type: 'address' },
    { name: 'offerHash', type: 'bytes32' },
  ],
} as const;

export const CLOSE_TYPES = {
  Close: [
    { name: 'sessionId', type: 'bytes32' },
    { name: 'configHash', type: 'bytes32' },
    { name: 'sequence', type: 'uint64' },
    { name: 'actor', type: 'address' },
    { name: 'reason', type: 'uint8' },
  ],
} as const;

/** `bigint` for amounts and times, per docs/contributing.md section 2.2. Never `number`. */
export interface Offer {
  sessionId: Hex;
  configHash: Hex;
  sequence: bigint;
  proposer: Address;
  quoteAmount: bigint;
  validUntil: bigint;
}

export interface Accept {
  sessionId: Hex;
  configHash: Hex;
  sequence: bigint;
  actor: Address;
  offerHash: Hex;
}

export interface Close {
  sessionId: Hex;
  configHash: Hex;
  sequence: bigint;
  actor: Address;
  reason: number;
}
