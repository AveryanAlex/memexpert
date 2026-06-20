import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { AdminSourceChannelRead, AdminTelegramChannelGroupRead, AdminTelegramSessionRead, UserRead } from '$lib/api/types';
import TelegramAdminPage from '../routes/admin/telegram/+page.svelte';

describe('/admin/telegram page', () => {
  it('renders sessions, grouped channels, required action forms, confirmations, and no secret echo', () => {
    const { body } = render(TelegramAdminPage, {
      props: {
        data: pageData(),
        form: null
      }
    });

    expect(body).toContain('Telegram admin');
    expect(body).toContain('Primary ingest');
    expect(body).toContain('StringSession stored');
    expect(body).not.toContain('secret-session-value');
    expect(body).toContain('action="?/createSession"');
    expect(body).toContain('name="string_session"');
    expect(body).toContain('action="?/validateSession"');
    expect(body).toContain('action="?/updateSession"');
    expect(body).toContain('action="?/deleteSession"');
    expect(body).toContain('Assigned channels become orphaned and non-indexable');
    expect(body).toContain('11111111-1111-4111-8111-111111111111');

    expect(body).toContain('action="?/addChannel"');
    expect(body).toContain('Orphaned, non-indexable');
    expect(body).toContain('Primary ingest (primary)');
    expect(body).toContain('Primary ingest (primary) (1)');
    expect(body).toContain('Orphaned Telegram channels (1)');
    expect(body).toContain('non-indexable group');
    expect(body).toContain('cannot enable crawler controls until assigned');
    expect(body).toContain('Assigned Source');
    expect(body).toContain('Orphaned Source');
    expect(body).toContain('indexable');
    expect(body).toContain('non-indexable');
    expect(body).toContain('action="?/updateChannel"');
    expect(body).toContain('action="?/assignChannel"');
    expect(body).toContain('Assign or move');
    expect(body).toContain('action="?/orphanChannel"');
    expect(body).toContain('Orphan and disable indexing');
    expect(body).toContain('action="?/markChannelDead"');
    expect(body).toContain('Paste the channel id to confirm');
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
});

function pageData() {
  const session = telegramSession();
  return {
    session: null,
    sessionError: null,
    adminUser: adminUser(),
    telegramAdmin: {
      sessions: [session],
      groups: [
        { telegram_session: session, is_orphaned: false, channels: [sourceChannel('22222222-2222-4222-8222-222222222222', session.id, 'Assigned Source')] },
        { telegram_session: null, is_orphaned: true, channels: [sourceChannel('33333333-3333-4333-8333-333333333333', null, 'Orphaned Source')] }
      ] satisfies AdminTelegramChannelGroupRead[]
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
    account_phone_hint: '+1***1234',
    has_string_session: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function sourceChannel(id: string, sessionId: string | null, title: string): AdminSourceChannelRead {
  return {
    id,
    platform: 'telegram',
    platform_id: id === '22222222-2222-4222-8222-222222222222' ? '-1001' : '-1002',
    username: title.toLowerCase().replaceAll(' ', '_'),
    title,
    subscriber_count: 1000,
    is_active: true,
    is_paused: false,
    catchup_enabled: sessionId !== null,
    live_enabled: sessionId !== null,
    engagement_enabled: sessionId !== null,
    catchup_message_limit: 500,
    telegram_session_id: sessionId,
    telegram_session_name: sessionId === null ? null : 'primary',
    is_orphaned: sessionId === null,
    is_indexable: sessionId !== null,
    last_read_post_id: null,
    last_fetched_at: null,
    operational_status: 'active',
    freshness_status: 'never_fetched',
    seconds_since_last_fetch: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}
