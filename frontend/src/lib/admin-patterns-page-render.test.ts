import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AdminBlockedPerceptualHashRead } from '$lib/api/types';
import PatternsPage from '../routes/admin/moderation/patterns/+page.svelte';

describe('/admin/moderation/patterns page', () => {
  it('renders active and inactive pattern summaries before progressively disclosed controls', () => {
    const active = pattern({ id: '11111111-1111-4111-8111-111111111111', is_active: true });
    const inactive = pattern({
      id: '22222222-2222-4222-8222-222222222222',
      is_active: false,
      reason: 'copyright',
      max_hamming_distance: 2,
      updated_at: '2026-07-10T23:45:30-04:00'
    });
    const { body } = render(PatternsPage, {
      props: { data: { patterns: [active, inactive], loadError: null }, form: null } as never
    });

    expect(body).toContain('Blocked media patterns');
    expect(body).toContain('pHash compares visual fingerprints, not exact file bytes.');
    expect(body).toContain('Active patterns');
    expect(body).toContain('Inactive patterns');
    expect(body).toContain('1 active');
    expect(body).toContain('1 inactive');
    expect(body).toContain('Reason');
    expect(body).toContain('State');
    expect(body).toContain('Match tolerance');
    expect(body).toContain('Exact pHash match');
    expect(body).toContain('Up to 2 differing pHash bits');
    expect(body).toContain('2026-07-11 03:45 UTC');
    expect(body).toContain('Pattern details and editing');
    expect(body).toContain('Raw perceptual hash');
    expect(body).toContain('Hash algorithm');
    expect(body).toContain('Bit size');
    expect(body).toContain('Maximum differing pHash bits');
    expect(body).toContain('Audit note (optional)');
    expect(body).toContain('Pattern lifecycle and deletion');
    expect(body).toContain('Type DEACTIVATE to confirm');
    expect(body).toContain('Type DELETE to confirm');
    expect(body).toContain('Type REACTIVATE to confirm');
    expect(body).toContain('Reactivate pattern');
    expect(body).toContain('action="?/updateBlockedPerceptualHash"');
    expect(body).toContain('action="?/deactivateBlockedPerceptualHash"');
    expect(body).toContain('action="?/reactivateBlockedPerceptualHash"');
    expect(body).toContain('action="?/deleteBlockedPerceptualHash"');
    expect(body).toContain('Add a blocked pattern');
    expect(body).toContain('Pattern fingerprint and match settings');
    expect(body).toContain('action="?/createBlockedPerceptualHash"');
    expect(body).not.toContain(`>${active.id}<`);
    expect(body).not.toContain(`>${inactive.id}<`);
    expect(body).not.toContain('Reactivate this pattern for new uploads');
    expect(body.match(/name="confirmation_phrase"(?=[^>]*required)/g)).toHaveLength(4);
    expect(body).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
  });

  it('renders empty lists and load/action failures clearly', () => {
    const { body } = render(PatternsPage, {
      props: {
        data: { patterns: [], loadError: 'Could not load blocked media patterns.' },
        form: { message: 'Blocked perceptual hash already exists for that algorithm and hash size.', error: true }
      } as never
    });

    expect(body).toContain('No active patterns');
    expect(body).toContain('No inactive patterns are being kept for reference.');
    expect(body).toContain('role="alert"');
    expect(body).toContain('Could not load blocked media patterns.');
    expect(body).toContain('Blocked perceptual hash already exists for that algorithm and hash size.');
  });
});

function pattern(overrides: Partial<AdminBlockedPerceptualHashRead> = {}): AdminBlockedPerceptualHashRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    perceptual_hash: 'abcdef1234567890',
    hash_algorithm: 'phash',
    hash_size: 64,
    max_hamming_distance: 0,
    reason: 'spam',
    note: 'Known duplicate artwork.',
    is_active: true,
    created_by_admin_user_id: '33333333-3333-4333-8333-333333333333',
    created_at: '2026-07-10T10:00:00Z',
    updated_at: '2026-07-10T11:00:00Z',
    ...overrides
  };
}
