import { describe, expect, it } from 'vitest';
import { formatAdminTimestamp } from './formatTimestamp';

describe('formatAdminTimestamp', () => {
  it('formats timestamps deterministically in UTC', () => {
    expect(formatAdminTimestamp('2026-07-10T23:45:30-04:00')).toBe('2026-07-11 03:45 UTC');
    expect(formatAdminTimestamp(new Date('2026-01-02T03:04:05Z'))).toBe('2026-01-02 03:04 UTC');
  });

  it('returns a stable label for invalid timestamps', () => {
    expect(formatAdminTimestamp('not-a-date')).toBe('Invalid date');
    expect(formatAdminTimestamp(new Date(Number.NaN))).toBe('Invalid date');
  });
});
