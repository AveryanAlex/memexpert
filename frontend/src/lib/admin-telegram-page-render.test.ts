import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { AdminTelegramSessionRead, UserRead } from '$lib/api/types';
import TelegramAdminPage from '../routes/admin/telegram/+page.svelte';

describe('/admin/telegram page', () => {
  it('renders sessions, a source-management link, required session action forms, confirmations, and no secret echo', () => {
    const { body } = render(TelegramAdminPage, {
      props: {
        data: pageData(),
        form: null
      }
    });

    expect(body).toContain('Telegram admin');
    expect(body).toContain('Primary ingest');
    expect(body).toContain('session key stored');
    expect(body).not.toContain('secret-session-value');
    expect(body).not.toContain('StringSession');
    expect(body).not.toContain('name="string_session"');
    expect(body).not.toContain('name="account_user_id"');
    expect(body).toContain('Start New Login');
    expect(body).not.toContain('action="?/createSession"');
    expect(body).not.toContain('Stable operator-facing key');
    expect(body).toContain('action="?/startQrLogin"');
    expect(body).toContain('action="?/startPhoneLogin"');
    expect(body).toContain('Telegram login');
    expect(body).toContain('phone → code → password');
    expect(body).toContain('Log in with phone');
    expect(body).toContain('Log in with QR');
    expect(body).not.toContain('Attempt id');
    expect(body).not.toContain('paste QR attempt id');
    expect(body).not.toContain('paste phone attempt id');
    expect(body).toContain('action="?/validateSession"');
    expect(body).toContain('action="?/updateSession"');
    expect(body).toContain('action="?/deleteSession"');
    expect(body).toContain('Assigned channels become orphaned and non-indexable');
    expect(body).toContain('11111111-1111-4111-8111-111111111111');

    expect(body).toContain('Manage 2 sources');
    expect(body).toContain('href="/admin/sources"');
    expect(body).toContain('Manage and validate source assignment from Sources.');
    expect(body).not.toContain('action="?/addChannel"');
    expect(body).not.toContain('Channels by Session');
    expect(body).not.toContain('action="?/updateChannel"');
    expect(body).not.toContain('action="?/assignChannel"');
    expect(body).not.toContain('action="?/orphanChannel"');
    expect(body).not.toContain('action="?/toggleChannel"');
    expect(body).not.toContain('action="?/markChannelDead"');
  });

  it('renders action errors and load errors as alert states', () => {
    const { body } = render(TelegramAdminPage, {
      props: {
        data: { ...pageData(), loadError: 'Could not load Telegram admin tools.' },
        form: { message: 'Paste the Telegram session id to delete it.', error: true }
      }
    });

    expect(body).toContain('role="alert"');
    expect(body).toContain('Paste the Telegram session id to delete it.');
    expect(body).toContain('Could not load Telegram admin tools.');
  });

  it('does not render the obsolete inline QR continue form', () => {
    const session = telegramSession();
    const attemptId = '44444444-4444-4444-8444-444444444444';
    const { body } = render(TelegramAdminPage, {
      props: {
        data: pageData(session),
        form: {
          message: 'Waiting for scan…',
          kind: 'qr',
          sessionId: session.id,
          attemptId,
          qrUrl: 'tg://login?token=fake-qr-token',
          expiresAt: '2026-01-01T00:10:00Z'
        }
      }
    });

    expect(body).toContain('Telegram login');
    expect(body).toContain('Log in with QR');
    expect(body).not.toContain('Step 2 of 2 · Scan QR');
    expect(body).not.toContain('data:image/svg+xml;utf8,');
    expect(body).not.toContain('alt="Telegram login QR code"');
    expect(body).not.toContain('Open Telegram login link');
    expect(body).not.toContain('href="tg://login?token=fake-qr-token"');
    expect(body).not.toContain('Refresh QR now');
    expect(body).not.toContain('action="?/completeQrLogin"');
    expect(body).not.toContain(`type="hidden" name="attempt_id" value="${attemptId}"`);
    expect(body).not.toContain('I scanned it — continue');
    expect(body).not.toContain('Attempt id');
  });

  it('renders an active phone code step with hidden attempt id', () => {
    const session = telegramSession();
    const attemptId = '55555555-5555-4555-8555-555555555555';
    const { body } = render(TelegramAdminPage, {
      props: {
        data: pageData(session),
        form: {
          message: 'Code sent to phone ending-1234. Enter the code from Telegram.',
          kind: 'phone_code',
          sessionId: session.id,
          attemptId,
          phoneHint: 'ending-1234',
          expiresAt: '2026-01-01T00:10:00Z'
        }
      }
    });

    expect(body).toContain('Step 2 of 3 · Enter code');
    expect(body).toContain('phone ending-1234');
    expect(body).toContain('action="?/completePhoneCodeLogin"');
    expect(body).toContain('name="code"');
    expect(body).toContain(`type="hidden" name="attempt_id" value="${attemptId}"`);
    expect(body).not.toContain('Attempt id');
  });

  it('renders an active QR password step with hidden attempt id', () => {
    const session = telegramSession();
    const attemptId = '66666666-6666-4666-8666-666666666666';
    const { body } = render(TelegramAdminPage, {
      props: {
        data: pageData(session),
        form: {
          message: 'Telegram requires the account password. Enter it to finish.',
          kind: 'password',
          method: 'qr',
          sessionId: session.id,
          attemptId
        }
      }
    });

    expect(body).toContain('Step 2 of 2 · Enter password');
    expect(body).toContain('Telegram password');
    expect(body).toContain('action="?/completePhonePasswordLogin"');
    expect(body).toContain('type="hidden" name="method" value="qr"');
    expect(body).toContain(`type="hidden" name="attempt_id" value="${attemptId}"`);
    expect(body).not.toContain('Attempt id');
  });
});

function pageData(session = telegramSession()) {
  return {
    session: null,
    sessionError: null,
    adminUser: adminUser(),
    telegramAdmin: {
      sessions: [session],
      sourceCount: 2
    },
    loadError: null
  };
}

function adminUser(): UserRead {
  return {
    id: '99999999-9999-4999-8999-999999999999',
    account_type: 'full',
    telegram_id: null,
    google_id: null,
    email: 'admin@example.test',
    email_verified_at: null,
    language: 'en',
    nsfw_enabled: false,
    token_nonce: 1,
    status: 'active',
    guest_expires_at: null,
    active_save_collection_id: null,
    is_admin: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function telegramSession(): AdminTelegramSessionRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    name: 'primary',
    display_name: 'Primary ingest',
    owned_channel_count: 1,
    status: 'active',
    enabled: true,
    flood_wait_until: null,
    live_listener_started_at: '2026-01-01T00:00:00Z',
    last_heartbeat_at: '2026-01-01T00:05:00Z',
    last_error_class: null,
    last_error_text: null,
    quarantined_at: null,
    live_enabled: true,
    catchup_enabled: true,
    engagement_enabled: true,
    max_requests_per_second: 1,
    account_user_id: 123,
    account_username: 'primary_user',
    account_phone_hint: 'ending-1234',
    has_string_session: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}
