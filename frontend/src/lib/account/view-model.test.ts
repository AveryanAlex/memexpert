import { describe, expect, it } from 'vitest';

import { accountBenefitText, accountStatusLabel, connectedProviderLabels } from './view-model';
import type { CurrentSessionRead } from '$lib/api/types';

describe('account header view model', () => {
  it('describes guest sessions as continuous linking with Telegram benefit', () => {
    const session = sessionPayload('guest');

    expect(accountStatusLabel(session)).toBe('Guest profile');
    expect(accountBenefitText(session)).toContain('Connect Telegram to keep saves and favorites');
    expect(connectedProviderLabels(session.linked_providers)).toEqual([]);
  });

  it('describes full sessions and connected providers', () => {
    const session = sessionPayload('full');
    session.linked_providers.email = 'user@example.com';
    session.linked_providers.google_linked = true;

    expect(accountStatusLabel(session)).toBe('Full profile');
    expect(accountBenefitText(session)).toContain('connected profile');
    expect(connectedProviderLabels(session.linked_providers)).toEqual(['Telegram', 'Google', 'Email']);
  });
});

function sessionPayload(accountType: 'full' | 'guest'): CurrentSessionRead {
  return {
    user: {
      id: '22222222-2222-4222-8222-222222222222',
      account_type: accountType,
      telegram_id: accountType === 'full' ? 123 : null,
      google_id: null,
      email: null,
      email_verified_at: null,
      language: 'any',
      nsfw_enabled: false,
      token_nonce: 0,
      status: 'active',
      guest_expires_at: accountType === 'guest' ? '2026-07-12T00:00:00Z' : null,
      active_save_collection_id: null,
      is_admin: accountType === 'full',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: accountType === 'full'
    }
  };
}
