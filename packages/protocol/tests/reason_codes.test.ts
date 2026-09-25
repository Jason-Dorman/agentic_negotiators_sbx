/**
 * The TypeScript reason table against `fixtures/reason_codes.v1.json`.
 *
 * The Python half is checked in `test_reason_codes.py`. The fixture is the single source, so a code
 * added on one side alone fails the build rather than producing a timeline sentence that describes
 * a different walk-away than the chain recorded.
 */

import { describe, expect, it } from 'vitest';

import fixture from '../fixtures/reason_codes.v1.json';
import {
  ABORT_REASONS,
  ABORT_REASON_CODES,
  CLOSE_REASONS,
  CLOSE_REASON_CODES,
  UnknownReasonCodeError,
  abortReasonName,
  closeReasonName,
} from '../src/index.js';

const expected = (entries: readonly { code: number; name: string }[]): Record<number, string> =>
  Object.fromEntries(entries.map(({ code, name }) => [code, name]));

describe('the tables match the fixture', () => {
  it('for close reasons', () => {
    expect({ ...CLOSE_REASONS }).toEqual(expected(fixture.close_reasons));
  });

  it('for abort reasons', () => {
    expect({ ...ABORT_REASONS }).toEqual(expected(fixture.abort_reasons));
  });

  it('over the contiguous ranges the contract enforces', () => {
    // `closeSession` accepts 1..3 and `abortSession` 1..4, as literal comparisons against
    // MAX_CLOSE_REASON and MAX_ABORT_REASON. A gap or a zero here would be a code the contract
    // refuses and the interface believes in.
    expect(Object.keys(CLOSE_REASONS).map(Number).sort()).toEqual([1, 2, 3]);
    expect(Object.keys(ABORT_REASONS).map(Number).sort()).toEqual([1, 2, 3, 4]);
  });

  it('with reverse maps that are exact inverses', () => {
    for (const [code, name] of Object.entries(CLOSE_REASONS)) {
      expect(CLOSE_REASON_CODES[name]).toBe(Number(code));
    }
    for (const [code, name] of Object.entries(ABORT_REASONS)) {
      expect(ABORT_REASON_CODES[name]).toBe(Number(code));
    }
  });

  it('sharing no name between the two namespaces', () => {
    // Overlapping integers, separate namespaces: a shared name would make a rendered reason
    // ambiguous about who ended the session.
    const shared = Object.keys(CLOSE_REASON_CODES).filter((name) => name in ABORT_REASON_CODES);
    expect(shared).toEqual([]);
  });
});

describe('rendering', () => {
  it('renders every close code', () => {
    for (const { code, name } of fixture.close_reasons) {
      expect(closeReasonName(code)).toBe(name);
    }
  });

  it('renders every abort code', () => {
    for (const { code, name } of fixture.abort_reasons) {
      expect(abortReasonName(code)).toBe(name);
    }
  });

  it.each([0, 4, 5, -1, 255])('throws on close code %i', (code) => {
    // Throws rather than rendering "unknown". A code outside the table means this package and the
    // deployed contract disagree, and showing that as an ordinary outcome would hide a protocol
    // mismatch behind a plausible-looking timeline.
    expect(() => closeReasonName(code)).toThrow(UnknownReasonCodeError);
  });

  it.each([0, 5, -1, 255])('throws on abort code %i', (code) => {
    expect(() => abortReasonName(code)).toThrow(UnknownReasonCodeError);
  });
});
