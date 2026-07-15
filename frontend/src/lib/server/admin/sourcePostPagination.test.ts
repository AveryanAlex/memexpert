import { describe, expect, it } from 'vitest';
import { MAX_SOURCE_POST_PAGE, sourcePostPageFromSearchParam, sourcePostStatusFromSearchParam } from './sourcePostPagination';

describe('source post pagination', () => {
  it('accepts positive pages and normalizes invalid values', () => {
    expect(sourcePostPageFromSearchParam('3')).toBe(3);
    expect(sourcePostPageFromSearchParam(null)).toBe(1);
    expect(sourcePostPageFromSearchParam('0')).toBe(1);
    expect(sourcePostPageFromSearchParam('1.5')).toBe(1);
  });

  it('clamps pages before offset arithmetic becomes unsafe', () => {
    expect(sourcePostPageFromSearchParam(String(Number.MAX_SAFE_INTEGER))).toBe(MAX_SOURCE_POST_PAGE);
  });

  it('accepts only the server-supported recovery status filters', () => {
    expect(sourcePostStatusFromSearchParam('failed')).toBe('failed');
    expect(sourcePostStatusFromSearchParam('processing')).toBe('processing');
    expect(sourcePostStatusFromSearchParam('indexed')).toBeNull();
    expect(sourcePostStatusFromSearchParam(null)).toBeNull();
  });
});
