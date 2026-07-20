/** Generate a time-sortable RFC 9562 UUIDv7 for an idempotent client event. */
export function uuidV7(
  timestamp = Date.now(),
  fillRandom: (bytes: Uint8Array) => Uint8Array = (bytes) => crypto.getRandomValues(bytes)
): string {
  const bytes = fillRandom(new Uint8Array(16));
  const milliseconds = Math.min(Math.max(0, Math.trunc(timestamp)), 0xffffffffffff);

  let remaining = milliseconds;
  for (let index = 5; index >= 0; index -= 1) {
    bytes[index] = remaining % 256;
    remaining = Math.floor(remaining / 256);
  }
  bytes[6] = 0x70 | (bytes[6] & 0x0f);
  bytes[8] = 0x80 | (bytes[8] & 0x3f);

  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
