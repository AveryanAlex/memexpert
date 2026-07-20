import { describe, expect, it } from 'vitest';
import type { AdminRecoveryActionCandidateRead, AdminRecoveryWorkRead } from '$lib/api/types';
import {
  recoveryActionRequirements,
  recoveryBatchAcknowledgements,
  recoveryCapabilityLabel,
  recoveryActionsForWork,
  recoveryDefaultBatchCapability,
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
      section: 'needs_attention',
      bucket: 'retryable',
      kind: 'pipeline_stage',
      source: '@memach',
      stage: 'ocr',
      reason: 'ocr_timeout',
      query: 'file-1',
      cursor: 'next',
      jobCursor: null
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
    expect(recoveryCapabilityLabel('replay_stage')).toBe('Replay stage');
    expect(recoveryCapabilityLabel('regenerate_derivatives')).toBe('Regenerate derivatives');
    expect(recoveryDefaultBatchCapability([
      { capabilities: [] },
      { capabilities: ['resume_backfill'] },
      { capabilities: ['retry_stage'] }
    ])).toBe('resume_backfill');
    expect(recoveryDefaultBatchCapability([])).toBe('retry_stage');
  });

  it('keeps every backend-declared action and supplies rollout-safe defaults', () => {
    const actions = recoveryActionsForWork({
      capabilities: ['retry_stage'],
      available_actions: undefined,
      actions: [
        {
          capability: 'replay_stage',
          available: true,
          scopes: ['stage_only', 'stage_and_dependents'],
          downstream_stages: ['embed', 'classify']
        },
        {
          capability: 'regenerate_derivatives',
          available: false,
          blocked_prerequisites: [{ code: 'missing_original', message: 'The original object is missing.' }]
        }
      ]
    });

    expect(actions).toHaveLength(2);
    expect(actions[0]).toMatchObject({ default_scope: 'stage_only', default_retry_limit: 3 });
    expect(actions[1].blocked_prerequisites).toEqual([
      { code: 'missing_original', message: 'The original object is missing.' }
    ]);
  });

  it('selects risks and acknowledgements from the effective replay scope', () => {
    const action: AdminRecoveryActionCandidateRead = {
      capability: 'replay_stage',
      available: true,
      scopes: ['stage_only', 'stage_and_dependents'],
      warnings: ['Legacy default-scope warning.'],
      required_acknowledgements: [],
      scope_requirements: {
        stage_only: {
          warnings: ['Stage-only replay can leave downstream data stale.'],
          risks: [],
          required_acknowledgements: []
        },
        stage_and_dependents: {
          warnings: [],
          risks: [
            'External provider output or semantic merge results may differ from the previous successful run.'
          ],
          required_acknowledgements: ['terminal_override']
        }
      }
    };

    expect(recoveryActionRequirements(action, 'stage_only')).toEqual({
      warnings: ['Stage-only replay can leave downstream data stale.'],
      risks: [],
      required_acknowledgements: []
    });
    expect(recoveryActionRequirements(action, 'stage_and_dependents')).toEqual({
      warnings: [],
      risks: [
        'External provider output or semantic merge results may differ from the previous successful run.'
      ],
      required_acknowledgements: [
        {
          key: 'terminal_override',
          label: 'I acknowledge that this terminal failure is being overridden for an audited replay.'
        }
      ]
    });

    const selected: Pick<
      AdminRecoveryWorkRead,
      'actions' | 'available_actions' | 'capabilities'
    >[] = [{ capabilities: ['retry_stage'], actions: [action] }];
    expect(recoveryBatchAcknowledgements(selected, 'replay_stage', 'stage_only')).toEqual([]);
    expect(recoveryBatchAcknowledgements(selected, 'replay_stage', 'stage_and_dependents')).toEqual([
      {
        key: 'terminal_override',
        label: 'I acknowledge that this terminal failure is being overridden for an audited replay.'
      }
    ]);
  });
});
