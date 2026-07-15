import { describe, expect, it } from 'vitest';
import {
  recoveryCapabilityLabel,
  recoveryFiltersFromUrl,
  recoveryHref,
  recoveryPrimaryCapability,
  recoveryWorkHref
} from './view-model';

describe('admin recovery view model', () => {
  it('normalizes supported URL-backed filters and rejects unknown enums', () => {
    const filters = recoveryFiltersFromUrl(
      new URL('https://example.test/admin/recovery?bucket=retryable&kind=pipeline_stage&source=%40memach&stage=ocr&reason=ocr_timeout&age=24h&q=file-1&cursor=next&snapshot_at=2026-07-15T00%3A00%3A00Z')
    );

    expect(filters).toEqual({
      bucket: 'retryable',
      kind: 'pipeline_stage',
      source: '@memach',
      stage: 'ocr',
      reason: 'ocr_timeout',
      query: 'file-1',
      cursor: 'next'
    });

    const invalid = recoveryFiltersFromUrl(
      new URL('https://example.test/admin/recovery?bucket=unknown&kind=service&age=forever')
    );
    expect(invalid.bucket).toBeNull();
    expect(invalid.kind).toBeNull();
  });

  it('builds stable pagination/detail links and resets only requested fields', () => {
    const filters = recoveryFiltersFromUrl(
      new URL('https://example.test/admin/recovery?bucket=stuck&q=job-1')
    );
    expect(recoveryHref(filters, { cursor: 'opaque' })).toBe(
      '/admin/recovery?bucket=stuck&q=job-1&cursor=opaque'
    );
    expect(recoveryWorkHref({ kind: 'source_post', id: 'post/id' })).toBe(
      '/admin/recovery/work/source_post/post%2Fid'
    );
  });

  it('uses backend capabilities and deterministic priority for the primary action', () => {
    expect(recoveryPrimaryCapability(['archive_dead_letter', 'recover_dead_letter'])).toBe(
      'recover_dead_letter'
    );
    expect(recoveryPrimaryCapability([])).toBeNull();
    expect(recoveryCapabilityLabel('resume_backfill')).toBe('Resume backfill');
    expect(recoveryCapabilityLabel('retry_stage')).toBe('Retry stage');
  });
});
