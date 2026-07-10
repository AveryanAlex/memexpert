import { describe, expect, it } from 'vitest';
import type { AdminTelegramSessionRead } from '$lib/api/types';
import { loginStateForAccount, toTelegramAccountViewModel } from './view-model';

describe('Telegram account view model', () => {
  it('maps every account state to the safe routine repair action', () => {
    const now = new Date('2026-01-01T01:00:00Z');

    expect(toTelegramAccountViewModel(account(), now)).toMatchObject({ status: 'Ready', primaryAction: 'Validate' });
    expect(toTelegramAccountViewModel(account({ status: 'auth_required', has_string_session: false }), now)).toMatchObject({
      status: 'Sign-in needed',
      primaryAction: 'Connect'
    });
    expect(toTelegramAccountViewModel(account({ status: 'flood_wait', flood_wait_until: '2026-01-01T01:30:00Z' }), now)).toMatchObject({
      status: 'Temporarily rate-limited',
      primaryAction: null,
      statusDetail: 'Wait for Telegram’s rate limit to end before taking account actions.'
    });
    expect(toTelegramAccountViewModel(account({ status: 'quarantined', quarantined_at: '2026-01-01T00:30:00Z' }), now)).toMatchObject({
      status: 'Needs attention',
      primaryAction: 'Validate',
      canReconnect: true
    });
    expect(toTelegramAccountViewModel(account({ enabled: false }), now)).toMatchObject({ status: 'Disabled', primaryAction: 'Enable' });
    expect(toTelegramAccountViewModel(account({ status: 'stopped' }), now)).toMatchObject({ status: 'Stopped', primaryAction: 'Resume' });
  });

  it('distinguishes current and expired rate-limit timestamps without reintroducing Telegram I/O', () => {
    const now = new Date('2026-01-01T01:00:00Z');

    expect(toTelegramAccountViewModel(account({ flood_wait_until: '2026-01-01T01:00:01Z' }), now)).toMatchObject({
      status: 'Temporarily rate-limited',
      primaryAction: null
    });
    expect(toTelegramAccountViewModel(account({ status: 'flood_wait', flood_wait_until: '2026-01-01T00:59:59Z' }), now)).toMatchObject({
      status: 'Needs attention',
      primaryAction: 'Validate',
      canReconnect: true
    });
    expect(toTelegramAccountViewModel(account({ flood_wait_until: '2026-01-01T00:59:59Z' }), now)).toMatchObject({
      status: 'Ready',
      primaryAction: 'Validate'
    });
    expect(toTelegramAccountViewModel(account({ enabled: false, flood_wait_until: '2026-01-01T01:00:01Z' }), now)).toMatchObject({
      status: 'Temporarily rate-limited',
      primaryAction: null
    });
    expect(toTelegramAccountViewModel(account({ status: 'stopped', flood_wait_until: '2026-01-01T01:00:01Z' }), now)).toMatchObject({
      status: 'Temporarily rate-limited',
      primaryAction: null
    });
    expect(toTelegramAccountViewModel(account({ enabled: false, flood_wait_until: '2026-01-01T00:59:59Z' }), now)).toMatchObject({
      status: 'Disabled',
      primaryAction: 'Enable'
    });
    expect(toTelegramAccountViewModel(account({ status: 'stopped', flood_wait_until: '2026-01-01T00:59:59Z' }), now)).toMatchObject({
      status: 'Stopped',
      primaryAction: 'Resume'
    });
  });

  it('keeps account identity, source count, heartbeat, and concise errors visible', () => {
    const model = toTelegramAccountViewModel(
      account({
        owned_channel_count: 2,
        last_heartbeat_at: '2026-01-01T00:55:00Z',
        last_error_class: 'FloodWaitError',
        last_error_text: 'Try again after the requested delay.'
      }),
      new Date('2026-01-01T01:00:00Z')
    );

    expect(model).toMatchObject({
      identity: '@primary_account',
      sourceCountLabel: '2 sources',
      heartbeatLabel: '5m ago',
      errorSummary: 'FloodWaitError',
      providerDetailsHidden: true
    });
  });

  it('does not expose raw phone numbers, credential-like text, or arbitrary provider detail through view data', () => {
    const model = toTelegramAccountViewModel(
      account({
        name: 'StringSession(secret-session-value)',
        display_name: 'Encrypted session value',
        account_username: null,
        account_phone_hint: '+15551234567',
        last_error_text: 'x7!',
        last_error_class: 'gAAAAABl1y4t9vKTzJB4B73AHKWKcvv6Yx2YFleu0Xxn2S4w'
      })
    );
    const renderedValues = JSON.stringify(model);

    expect(model.identity).toBe('Telegram identity unavailable');
    expect(model.displayName).toBe('Telegram account');
    expect(model.technicalName).toBe('Redacted technical name');
    expect(renderedValues).not.toContain('secret-session-value');
    expect(renderedValues).not.toContain('StringSession');
    expect(renderedValues).not.toContain('Encrypted session value');
    expect(renderedValues).not.toContain('+15551234567');
    expect(renderedValues).not.toContain('not-for-html');
    expect(renderedValues).not.toContain('gAAAAABl1y4t9vKTzJB4B73AHKWKcvv6Yx2YFleu0Xxn2S4w');
    expect(renderedValues).not.toContain('x7!');
    expect(model.errorSummary).toBe('Sensitive account detail was redacted.');
    expect(model.providerDetailsHidden).toBe(true);
  });

  it('uses the supplied server load clock for deterministic heartbeat text', () => {
    const accountWithHeartbeat = account({ last_heartbeat_at: '2026-01-01T00:59:00Z' });
    const loadedAt = new Date('2026-01-01T01:00:00Z');

    expect(toTelegramAccountViewModel(accountWithHeartbeat, loadedAt).heartbeatLabel).toBe('1m ago');
    expect(toTelegramAccountViewModel(accountWithHeartbeat, loadedAt).heartbeatLabel).toBe('1m ago');
  });

  it('keeps failed phone-code and password steps attached to their account without exposing their submitted values', () => {
    const accountId = account().id;
    const phoneCodeState = loginStateForAccount(
      { kind: 'phone_code', sessionId: accountId, attemptId: 'attempt-1', phoneHint: 'ending-1234', error: true, message: 'Telegram could not verify that code.' },
      accountId,
      null
    );
    const passwordState = loginStateForAccount(
      { kind: 'password', method: 'phone', sessionId: accountId, attemptId: 'attempt-1', phoneHint: 'ending-1234', error: true, message: 'password=should-not-render' },
      accountId,
      null
    );

    expect(phoneCodeState).toMatchObject({ kind: 'phone_code', error: true, attemptId: 'attempt-1' });
    expect(passwordState).toMatchObject({ kind: 'password', error: true, attemptId: 'attempt-1' });
    expect(JSON.stringify(passwordState)).not.toContain('should-not-render');
  });
});

function account(overrides: Partial<AdminTelegramSessionRead> = {}): AdminTelegramSessionRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    name: 'primary',
    display_name: 'Primary account',
    owned_channel_count: 1,
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
    account_user_id: 123,
    account_username: 'primary_account',
    account_phone_hint: 'ending-1234',
    has_string_session: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}
