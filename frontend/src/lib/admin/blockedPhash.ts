const MAX_PHASH_HEX_LENGTH = 64;
const HEX_PATTERN = /^[0-9a-f]+$/;

export interface BlockedPhashFormPayload {
  perceptual_hash: string;
  hash_algorithm: string;
  hash_size: number;
  max_hamming_distance: number;
  reason: string;
  note: string | null;
  is_active: boolean;
}

export function buildBlockedPhashPayload(input: {
  perceptualHash: string;
  hashAlgorithm?: string | null;
  maxHammingDistance: number;
  reason: string;
  note?: string | null;
  isActive?: boolean;
}): BlockedPhashFormPayload {
  const perceptualHash = input.perceptualHash.trim().toLowerCase();
  if (!perceptualHash) {
    throw new Error('perceptual_hash is required.');
  }
  if (perceptualHash.length > MAX_PHASH_HEX_LENGTH || !HEX_PATTERN.test(perceptualHash)) {
    throw new Error('perceptual_hash must be 1-64 hexadecimal characters.');
  }

  const hashSize = perceptualHash.length * 4;
  if (!Number.isInteger(input.maxHammingDistance) || input.maxHammingDistance < 0) {
    throw new Error('max_hamming_distance must be zero or greater.');
  }
  if (input.maxHammingDistance > hashSize) {
    throw new Error('max_hamming_distance cannot exceed hash_size.');
  }

  const hashAlgorithm = (input.hashAlgorithm || 'phash').trim().toLowerCase();
  if (!hashAlgorithm) {
    throw new Error('hash_algorithm is required.');
  }
  if (hashAlgorithm !== 'phash') {
    throw new Error('Only the phash perceptual hash algorithm is currently supported.');
  }

  return {
    perceptual_hash: perceptualHash,
    hash_algorithm: hashAlgorithm,
    hash_size: hashSize,
    max_hamming_distance: input.maxHammingDistance,
    reason: input.reason,
    note: input.note?.trim() || null,
    is_active: input.isActive ?? true
  };
}
