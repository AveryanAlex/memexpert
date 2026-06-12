import { describe, expect, it } from 'vitest';

import { buildBlockedPhashPayload } from './blockedPhash';

describe('blocked pHash admin payloads', () => {
  it('normalizes uppercase hex and derives hash size', () => {
    expect(
      buildBlockedPhashPayload({
        perceptualHash: ' ABCDEF1234567890 ',
        maxHammingDistance: 2,
        reason: 'spam',
        note: '  block this pattern  '
      })
    ).toEqual({
      perceptual_hash: 'abcdef1234567890',
      hash_algorithm: 'phash',
      hash_size: 64,
      max_hamming_distance: 2,
      reason: 'spam',
      note: 'block this pattern',
      is_active: true
    });
  });

  it('rejects malformed hashes and impossible distance thresholds', () => {
    expect(() =>
      buildBlockedPhashPayload({ perceptualHash: 'not-hex', maxHammingDistance: 0, reason: 'spam' })
    ).toThrow('hexadecimal');
    expect(() =>
      buildBlockedPhashPayload({ perceptualHash: 'ff', maxHammingDistance: 9, reason: 'spam' })
    ).toThrow('cannot exceed hash_size');
    expect(() =>
      buildBlockedPhashPayload({
        perceptualHash: 'ff',
        hashAlgorithm: 'average_hash',
        maxHammingDistance: 0,
        reason: 'spam'
      })
    ).toThrow('phash');
  });
});
