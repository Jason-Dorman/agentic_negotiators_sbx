/**
 * The TypeScript third of the three-language digest check (docs/test_strategy.md section 5).
 *
 * `fixtures/eip712.v1.json` is written by `tools/generate_eip712_fixtures.py`, which computes every
 * digest by hand from docs/protocol.md sections 3 and 4. pytest checks it with `eth_account`'s
 * implementation, Forge with OpenZeppelin's, and this file with viem's. Four implementations
 * agreeing is the evidence; any one disagreeing fails the build in the language that drifted.
 *
 * The type definitions under test are the ones the web client will use to verify an export, so a
 * mismatch here is a mismatch in what a viewer would be shown.
 */

import { describe, expect, it } from 'vitest';
import {
  encodeAbiParameters,
  hashDomain,
  hashTypedData,
  keccak256,
  recoverTypedDataAddress,
  toBytes,
  type Address,
  type Hex,
} from 'viem';

import fixture from '../fixtures/eip712.v1.json';
import {
  ACCEPT_TYPES,
  CLOSE_TYPES,
  EIP712_DOMAIN_TYPES,
  OFFER_TYPES,
  domain,
  type Accept,
  type Close,
  type Offer,
} from '../src/index.js';

const DOMAIN = domain(fixture.domain.chain_id, fixture.domain.verifying_contract as Address);

const offer: Offer = {
  sessionId: fixture.messages.offer.message.session_id as Hex,
  configHash: fixture.messages.offer.message.config_hash as Hex,
  sequence: BigInt(fixture.messages.offer.message.sequence),
  proposer: fixture.messages.offer.message.proposer as Address,
  quoteAmount: BigInt(fixture.messages.offer.message.quote_amount),
  validUntil: BigInt(fixture.messages.offer.message.valid_until),
};

const acceptance: Accept = {
  sessionId: fixture.messages.accept.message.session_id as Hex,
  configHash: fixture.messages.accept.message.config_hash as Hex,
  sequence: BigInt(fixture.messages.accept.message.sequence),
  actor: fixture.messages.accept.message.actor as Address,
  offerHash: fixture.messages.accept.message.offer_hash as Hex,
};

const closure: Close = {
  sessionId: fixture.messages.close.message.session_id as Hex,
  configHash: fixture.messages.close.message.config_hash as Hex,
  sequence: BigInt(fixture.messages.close.message.sequence),
  actor: fixture.messages.close.message.actor as Address,
  reason: fixture.messages.close.message.reason,
};

describe('EIP-712 domain', () => {
  it('is the one in the protocol document', () => {
    // A change to any of these four values changes every digest the system has ever produced.
    expect(DOMAIN.name).toBe('AgentNegotiationSandbox');
    expect(DOMAIN.version).toBe('1');
    expect(DOMAIN.chainId).toBe(31337n);
  });

  it('hashes to the fixture separator', () => {
    expect(hashDomain({ domain: DOMAIN, types: EIP712_DOMAIN_TYPES })).toBe(
      fixture.domain_separator,
    );
  });

  it('derives its type hashes from the fixture type strings', () => {
    // The strings are byte-exact, including the absence of a space after each comma: the string
    // itself is hashed, so a cosmetic difference is a different protocol.
    for (const [kind, typeString] of Object.entries(fixture.type_strings)) {
      expect(keccak256(toBytes(typeString))).toBe(
        fixture.type_hashes[kind as keyof typeof fixture.type_hashes],
      );
    }
  });
});

describe('configHash', () => {
  it('binds the session to this exchange and its token pair', () => {
    // docs/protocol.md section 3: abi.encode, never concatenation, never JSON hashing. The two
    // token addresses enter from the exchange's immutables, which is what stops a session signed
    // for one deployment being replayed against another (A07).
    const encoded = encodeAbiParameters(
      [
        { type: 'bytes32' },
        { type: 'address' },
        { type: 'address' },
        { type: 'address' },
        { type: 'address' },
        { type: 'uint256' },
        { type: 'uint64' },
        { type: 'uint16' },
      ],
      [
        fixture.session_config.session_id as Hex,
        fixture.session_config.buyer as Address,
        fixture.session_config.seller as Address,
        fixture.deployment.base_token as Address,
        fixture.deployment.quote_token as Address,
        BigInt(fixture.session_config.base_amount),
        BigInt(fixture.session_config.expires_at),
        fixture.session_config.max_offers,
      ],
    );

    expect(keccak256(encoded)).toBe(fixture.config_hash);
  });
});

describe('typed message digests', () => {
  it('matches the fixture for an Offer', () => {
    expect(
      hashTypedData({
        domain: DOMAIN,
        types: OFFER_TYPES,
        primaryType: 'Offer',
        message: offer,
      }),
    ).toBe(fixture.messages.offer.digest);
  });

  it('matches the fixture for an Accept', () => {
    expect(
      hashTypedData({
        domain: DOMAIN,
        types: ACCEPT_TYPES,
        primaryType: 'Accept',
        message: acceptance,
      }),
    ).toBe(fixture.messages.accept.digest);
  });

  it('matches the fixture for a Close', () => {
    expect(
      hashTypedData({
        domain: DOMAIN,
        types: CLOSE_TYPES,
        primaryType: 'Close',
        message: closure,
      }),
    ).toBe(fixture.messages.close.digest);
  });

  it('changes when any field changes', () => {
    // The negative half. Without it, a bug that hashed every message to the same value would
    // satisfy all three assertions above.
    const mutated = hashTypedData({
      domain: DOMAIN,
      types: OFFER_TYPES,
      primaryType: 'Offer',
      message: { ...offer, quoteAmount: offer.quoteAmount + 1n },
    });

    expect(mutated).not.toBe(fixture.messages.offer.digest);
  });

  it('changes when the chain changes', () => {
    // A07: the same fields on another chain are a different digest, so a signature cannot be
    // replayed from Anvil onto Sepolia.
    const otherChain = hashTypedData({
      domain: domain(11155111, fixture.domain.verifying_contract as Address),
      types: OFFER_TYPES,
      primaryType: 'Offer',
      message: offer,
    });

    expect(otherChain).not.toBe(fixture.messages.offer.digest);
  });

  it('changes when the verifying contract changes', () => {
    const otherContract = hashTypedData({
      domain: domain(fixture.domain.chain_id, fixture.deployment.base_token as Address),
      types: OFFER_TYPES,
      primaryType: 'Offer',
      message: offer,
    });

    expect(otherContract).not.toBe(fixture.messages.offer.digest);
  });
});

describe('signature recovery', () => {
  it('recovers the offer to the proposer the message names', async () => {
    // Step 5 of the verification order, off-chain: the contract requires the recovered signer to
    // equal the address inside the message, and this is the same check a viewer can run.
    const recovered = await recoverTypedDataAddress({
      domain: DOMAIN,
      types: OFFER_TYPES,
      primaryType: 'Offer',
      message: offer,
      signature: fixture.messages.offer.signature as Hex,
    });

    expect(recovered).toBe(offer.proposer);
    expect(recovered).toBe(fixture.participants.buyer.address);
  });

  it('recovers the acceptance to the counterparty', async () => {
    const recovered = await recoverTypedDataAddress({
      domain: DOMAIN,
      types: ACCEPT_TYPES,
      primaryType: 'Accept',
      message: acceptance,
      signature: fixture.messages.accept.signature as Hex,
    });

    expect(recovered).toBe(acceptance.actor);
    expect(recovered).toBe(fixture.participants.seller.address);
    // Self-acceptance is invalid (docs/protocol.md rule 5.6), so the pair must come from
    // opposite sides to be executable at all.
    expect(recovered).not.toBe(offer.proposer);
  });

  it('recovers the close to the party that signed it', async () => {
    const recovered = await recoverTypedDataAddress({
      domain: DOMAIN,
      types: CLOSE_TYPES,
      primaryType: 'Close',
      message: closure,
      signature: fixture.messages.close.signature as Hex,
    });

    expect(recovered).toBe(closure.actor);
  });

  it('does not recover the named party from a mutated message', async () => {
    const recovered = await recoverTypedDataAddress({
      domain: DOMAIN,
      types: OFFER_TYPES,
      primaryType: 'Offer',
      message: { ...offer, quoteAmount: offer.quoteAmount + 1n },
      signature: fixture.messages.offer.signature as Hex,
    });

    expect(recovered).not.toBe(offer.proposer);
  });
});

describe('the acceptance links to the offer digest', () => {
  it('references the offer full digest, domain included', () => {
    // docs/protocol.md section 4. Both signatures then authorise the same trade on the same chain
    // and the same contract; a linkage over the struct hash alone would not.
    expect(acceptance.offerHash).toBe(fixture.messages.offer.digest);
  });
});
