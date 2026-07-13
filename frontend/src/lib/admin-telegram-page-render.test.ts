import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { AdminTelegramSessionRead, UserRead } from '$lib/api/types';
import TelegramAdminPage from '../routes/admin/telegram/+page.svelte';

describe('/admin/telegram page', () => {
  it('renders task-oriented Telegram accounts with compact health, progressive disclosure, and source management only', () => {
    const { body } = render(TelegramAdminPage, {
      props: {
        data: pageData(),
        form: null
      }
    });

    expect(body).toContain('Telegram accounts');
    expect(body).toContain('Connect a Telegram account');
    expect(body).toContain('Connect with QR');
    expect(body).toContain('Use phone instead');
    expect(body).toContain('Primary ingest');
    expect(body).toContain('@primary_user');
    expect(body).toContain('Ready');
    expect(body).toContain('1 source');
    expect(body).toContain('Last heartbeat');
    expect(body).toContain('Diagnostics');
    expect(body).toContain('Advanced settings');
    expect(body).toContain('Disconnect account');
    expect(body).toContain('Permanently deletes this database account record. 1 assigned source becomes unassigned and ingestion is disabled.');
    expect(body).toContain('Internal status');
    expect(body).toContain('Technical account name');
    expect(body).toContain('Account ID');
    expect(body).not.toContain('action="?/createSession"');
    expect(body).toContain('action="?/startQrLogin"');
    expect(body).toContain('action="?/startPhoneLogin"');
    expect(body).toContain('name="phone_number"');
    expect(body).not.toContain('Attempt id');
    expect(body).not.toContain('paste QR attempt id');
    expect(body).not.toContain('paste phone attempt id');
    expect(body).toContain('action="?/validateSession"');
    expect(body).toContain('action="?/updateSession"');
    expect(body).toContain('action="?/deleteSession"');
    expect(body).toContain('Type DISCONNECT to permanently delete this account');
    expect(body).toContain('placeholder="DISCONNECT"');
    expect(body).not.toContain(`placeholder="${telegramSession().id}"`);

    expect(body).toContain('Manage sources');
    expect(body).toContain('href="/admin/sources"');
    expect(body).not.toContain('action="?/addChannel"');
    expect(body).not.toContain('Channels by Session');
    expect(body).not.toContain('action="?/updateChannel"');
    expect(body).not.toContain('action="?/assignChannel"');
    expect(body).not.toContain('action="?/orphanChannel"');
    expect(body).not.toContain('action="?/toggleChannel"');
    expect(body).not.toContain('action="?/markChannelDead"');
  });

  it('does not render secret-like account data, full phone numbers, passwords, or visible attempt IDs', () => {
    const session = telegramSession({
      name: 'StringSession(secret-session-value)',
      display_name: 'Encrypted account value',
      account_username: null,
      account_phone_hint: '+15551234567',
      last_error_text: 'x7!'
    });
    const { body } = render(TelegramAdminPage, {
      props: {
        data: pageData(session),
        form: { message: 'password=not-for-html StringSession(secret-session-value)', error: true }
      }
    });

    expect(body).not.toContain('secret-session-value');
    expect(body).not.toContain('StringSession');
    expect(body).not.toContain('Encrypted account value');
    expect(body).not.toContain('+15551234567');
    expect(body).not.toContain('not-for-html');
    expect(body).not.toContain('x7!');
    expect(body).not.toContain('Attempt ID');
    expect(body).not.toContain('name="string_session"');
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

    expect(body).toContain('Connect with QR');
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

  it('keeps login failures attached to the account with a restart action', () => {
    const session = telegramSession({ status: 'auth_required', has_string_session: false });
    const { body } = render(TelegramAdminPage, {
      props: {
        data: pageData(session),
        form: {
          kind: 'login_error',
          method: 'qr',
          sessionId: session.id,
          message: 'QR sign-in could not start.',
          error: true
        }
      }
    });

    expect(body).toContain('Sign-in did not finish.');
    expect(body).toContain('Restart with QR');
    expect(body).toContain('action="?/startQrLogin"');
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
          method: 'phone',
          phoneHint: 'ending-1234',
          expiresAt: '2026-01-01T00:10:00Z',
          error: false
        }
      }
    });

    expect(body).toContain('Enter the Telegram code');
    expect(body).toContain('Phone ending 1234');
    expect(body).toContain('action="?/completePhoneCodeLogin"');
    expect(body).toContain('formaction="?/cancelLoginAttempt"');
    expect(body).toContain('name="code"');
    expect(body).toContain(`type="hidden" name="attempt_id" value="${attemptId}"`);
    expect(body).not.toContain('Attempt id');
  });

  it('renders standalone phone and password steps before a new account record exists', () => {
    const attemptId = '99999999-9999-4999-8999-999999999998';
    const noAccounts = { ...pageData(), telegramAdmin: { sessions: [] } };
    const code = render(TelegramAdminPage, {
      props: {
        data: noAccounts,
        form: {
          message: 'Telegram sent a verification code.',
          kind: 'phone_code',
          sessionId: null,
          attemptId,
          method: 'phone',
          phoneHint: 'ending-1234',
          error: false
        }
      }
    }).body;
    const password = render(TelegramAdminPage, {
      props: {
        data: noAccounts,
        form: {
          message: 'Telegram requires the account password.',
          kind: 'password',
          method: 'qr',
          sessionId: null,
          attemptId,
          error: false
        }
      }
    }).body;

    expect(code).toContain('Enter the Telegram code');
    expect(code).toContain('Phone ending 1234');
    expect(code).toContain(`type="hidden" name="attempt_id" value="${attemptId}"`);
    expect(code).not.toContain('name="session_id"');
    expect(password).toContain('Enter the Telegram password');
    expect(password).toContain('type="hidden" name="method" value="qr"');
    expect(password).not.toContain('name="session_id"');
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
          attemptId,
          error: false
        }
      }
    });

    expect(body).toContain('Enter the Telegram password');
    expect(body).toContain('Telegram password');
    expect(body).toContain('action="?/completePhonePasswordLogin"');
    expect(body).toContain('Cancel sign-in');
    expect(body).toContain('type="hidden" name="method" value="qr"');
    expect(body).toContain(`type="hidden" name="attempt_id" value="${attemptId}"`);
    expect(body).not.toContain('Attempt id');
  });

  it('keeps failed code and password continuation forms visible without rendering submitted secrets', () => {
    const session = telegramSession({ status: 'auth_required', has_string_session: false });
    const codeAttemptId = '77777777-7777-4777-8777-777777777777';
    const passwordAttemptId = '88888888-8888-4888-8888-888888888888';
    const code = render(TelegramAdminPage, {
      props: {
        data: pageData(session),
        form: {
          kind: 'phone_code',
          sessionId: session.id,
          attemptId: codeAttemptId,
          method: 'phone',
          phoneHint: 'ending-1234',
          error: true,
          message: 'Telegram could not verify that code. Check it and try again.'
        }
      }
    }).body;
    const password = render(TelegramAdminPage, {
      props: {
        data: pageData(session),
        form: {
          kind: 'password',
          method: 'phone',
          sessionId: session.id,
          attemptId: passwordAttemptId,
          phoneHint: 'ending-1234',
          error: true,
          message: 'password=not-for-html'
        }
      }
    }).body;

    expect(code).toContain('Telegram could not verify that code. Check it and try again.');
    expect(code).toContain('action="?/completePhoneCodeLogin"');
    expect(code).toContain(`type="hidden" name="attempt_id" value="${codeAttemptId}"`);
    expect(password).toContain('action="?/completePhonePasswordLogin"');
    expect(password).toContain(`type="hidden" name="attempt_id" value="${passwordAttemptId}"`);
    expect(password).not.toContain('not-for-html');
  });
});

function pageData(session = telegramSession()) {
  return {
    session: null,
    sessionError: null,
    adminUser: adminUser(),
    telegramAdmin: {
      sessions: [session]
    },
    loadedAt: '2026-01-01T00:10:00Z',
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

function telegramSession(overrides: Partial<AdminTelegramSessionRead> = {}): AdminTelegramSessionRead {
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
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}
