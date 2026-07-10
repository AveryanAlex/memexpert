import { describe, expect, it } from 'vitest';
import type { AdminTelegramSessionRead } from '$lib/api/types';
import { isReadyTelegramAccount } from './readiness';

describe('isReadyTelegramAccount', () => {
  const now = new Date('2026-07-10T12:00:00Z');

  it('accepts only enabled, active accounts with a stored session and no current restrictions', () => {
    expect(isReadyTelegramAccount(account(), now)).toBe(true);
    expect(isReadyTelegramAccount(account({ enabled: false }), now)).toBe(false);
    expect(isReadyTelegramAccount(account({ status: 'auth_required' }), now)).toBe(false);
    expect(isReadyTelegramAccount(account({ has_string_session: false }), now)).toBe(false);
    expect(isReadyTelegramAccount(account({ flood_wait_until: '2026-07-10T12:01:00Z' }), now)).toBe(false);
    expect(isReadyTelegramAccount(account({ quarantined_at: '2026-07-10T11:00:00Z' }), now)).toBe(false);
  });

  it('allows an elapsed flood wait but rejects an invalid restriction timestamp conservatively', () => {
    expect(isReadyTelegramAccount(account({ flood_wait_until: '2026-07-10T11:59:59Z' }), now)).toBe(true);
    expect(isReadyTelegramAccount(account({ flood_wait_until: 'not-a-date' }), now)).toBe(false);
  });
});

function account(overrides: Partial<AdminTelegramSessionRead> = {}): AdminTelegramSessionRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    name: 'primary',
    display_name: 'Primary account',
    owned_channel_count: 0,
    status: 'active',
    enabled: true,
    flood_wait_until: null,
    live_listener_started_at: null,
    last_heartbeat_at: null,
    last_error_class: null,
    last_error_text: null,
    quarantined_at: null,
    live_enabled: true,
    catchup_enabled: true,
    engagement_enabled: true,
    max_requests_per_second: 1,
    account_user_id: 1,
    account_username: 'primary',
    account_phone_hint: 'ending-1234',
    has_string_session: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}
