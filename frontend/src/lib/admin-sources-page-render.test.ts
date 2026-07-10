import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AdminSourceChannelRead, AdminTelegramSessionRead, ChannelSuggestionRead } from '$lib/api/types';
import AddSourceByReference from '$lib/features/admin/sources/AddSourceByReference.svelte';
import SourcesAdminPage from '../routes/admin/sources/+page.svelte';

describe('/admin/sources page', () => {
  it('renders supported suggestions, source health, and progressive source controls', () => {
    const { body } = render(SourcesAdminPage, {
      props: {
        data: {
          sourceAdmin: {
            suggestions: [telegramSuggestion(), suggestion({ id: 'suggestion-reddit', platform: 'reddit' }), suggestion({ id: 'suggestion-vk', platform: 'vk' })],
            sourceChannels: [
              sourceChannel(),
              sourceChannel({ id: 'source-paused', is_paused: true, operational_status: 'paused' }),
              sourceChannel({ id: 'source-account', telegram_session_id: null, telegram_session_name: null, is_orphaned: true, is_indexable: false }),
              sourceChannel({ id: 'source-stale', freshness_status: 'stale', seconds_since_last_fetch: 7_200 }),
              sourceChannel({ id: 'source-removed', is_active: false, operational_status: 'inactive' }),
              sourceChannel({ id: 'source-unavailable', telegram_session_id: 'backup-account', telegram_session_name: 'Backup ingest' }),
              sourceChannel({ id: 'source-reddit', platform: 'reddit', telegram_session_id: null, telegram_session_name: null, is_orphaned: true, is_indexable: false })
            ],
            telegramAccounts: [telegramAccount(), telegramAccount({ id: 'backup-account', display_name: 'Backup ingest', enabled: false })]
          },
          loadError: null
        },
        form: null
      } as never
    });

    expect(body).toContain('Source management');
    expect(body).toContain('Add Telegram source');
    expect(body).toContain('Channel link or @handle');
    expect(body).toContain('Telegram account');
    expect(body).toContain('action="?/addSourceByReference"');
    expect(body).toContain('value="22222222-2222-4222-8222-222222222222" selected');
    expect(body).toContain('Advanced settings');
    expect(body).toContain('name="catchup_message_limit"');
    expect(body).toContain('Suggested sources');
    expect(body).toContain('https://t.me/telegram_source');
    expect(body).toContain('Add this source');
    expect(body).toContain('action="?/reviewSuggestion"');
    expect(body).toContain('Reddit crawler support is unavailable');
    expect(body).toContain('VK crawler support is unavailable');
    expect(body).not.toContain('Add Reddit source');
    expect(body).not.toContain('Add VK source');

    expect(body).toContain('Healthy');
    expect(body).toContain('Paused');
    expect(body).toContain('Needs account');
    expect(body).toContain('Needs attention');
    expect(body).toContain('Crawler unavailable');
    expect(body).toContain('Removed');
    expect(body).toContain('Last fetched:</strong> 5m ago');
    expect(body).toContain('Account:</strong> Primary ingest');
    expect(body).toContain('Account:</strong> Backup ingest (unavailable)');
    expect(body).toContain('Account:</strong> Not applicable');
    expect(body).toContain('Current account:');
    expect(body).toContain('Choose a ready account before saving.');
    expect(body).toContain('action="?/toggleSourceChannel"');

    expect(body).toContain('Advanced manual source entry');
    expect(body).toContain('Diagnostics');
    expect(body).toContain('Ingestion settings');
    expect(body).toContain('Assignment');
    expect(body).toContain('Remove source');
    expect(body).toContain('action="?/updateSourceChannelIngestion"');
    expect(body).toContain('action="?/assignSourceChannel"');
    expect(body).toContain('action="?/orphanSourceChannel"');
    expect(body).toContain('action="?/validateSourceAccount"');
    expect(body).toContain('Validate source access');
    expect(body).toContain('Assignment note (optional)');
    expect(body).toContain('Reason (optional)');
    expect(body).toContain('Validation note (optional)');
    expect(body).toContain('action="?/markSourceChannelDead"');
    expect(body).toContain('Paste the source ID from Diagnostics to confirm.');
    expect(body).toContain('New sources are added without an account and with ingestion off.');
    expect(body).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
    expect(body).not.toContain('placeholder="11111111-1111-4111-8111-111111111111"');
  });

  it('renders source action and load failures as alerts', () => {
    const { body } = render(SourcesAdminPage, {
      props: {
        data: { sourceAdmin: { suggestions: [], sourceChannels: [], telegramAccounts: [] }, loadError: 'Could not load source management.' },
        form: { message: 'Source ID is required.', error: true }
      } as never
    });

    expect(body).toContain('role="alert"');
    expect(body).toContain('Source ID is required.');
    expect(body).toContain('Could not load source management.');
  });

  it('enables ingestion controls after a ready account is assigned to a manual source with ingestion off', () => {
    const { body } = render(SourcesAdminPage, {
      props: {
        data: {
          sourceAdmin: {
            suggestions: [],
            sourceChannels: [sourceChannel({ is_indexable: false, catchup_enabled: false, live_enabled: false, engagement_enabled: false })],
            telegramAccounts: [telegramAccount()]
          },
          loadError: null
        },
        form: null
      } as never
    });

    expect(body).toContain('Needs attention');
    expect(body).toContain('Ingestion is off.');
    expect(body).toMatch(/name="catchup_enabled" type="checkbox"(?![^>]*disabled)/);
    expect(body).toMatch(/name="live_enabled" type="checkbox"(?![^>]*disabled)/);
    expect(body).toMatch(/name="engagement_enabled" type="checkbox"(?![^>]*disabled)/);
    expect(body).toMatch(/name="catchup_message_limit" type="number"(?![^>]*\sdisabled(?:=|\s|>))/);
  });

  it('auto-selects only one ready account and reports when none are ready', () => {
    const oneReady = render(SourcesAdminPage, {
      props: {
        data: {
          sourceAdmin: {
            suggestions: [],
            sourceChannels: [],
            telegramAccounts: [telegramAccount(), telegramAccount({ id: 'disabled-account', enabled: false })]
          },
          loadError: null
        },
        form: null
      } as never
    }).body;
    expect(oneReady).toMatch(/<option value="22222222-2222-4222-8222-222222222222" selected="">Primary ingest<\/option>/);
    expect(oneReady).not.toContain('value="disabled-account"');

    const multipleReady = render(SourcesAdminPage, {
      props: {
        data: {
          sourceAdmin: {
            suggestions: [],
            sourceChannels: [],
            telegramAccounts: [telegramAccount(), telegramAccount({ id: 'ready-backup', display_name: 'Ready backup' })]
          },
          loadError: null
        },
        form: null
      } as never
    }).body;
    expect(multipleReady).toContain('Choose which ready account should fetch this source.');
    expect(multipleReady).not.toMatch(/<option value="(?:22222222-2222-4222-8222-222222222222|ready-backup)" selected/);

    const noneReady = render(SourcesAdminPage, {
      props: {
        data: {
          sourceAdmin: { suggestions: [], sourceChannels: [], telegramAccounts: [telegramAccount({ enabled: false })] },
          loadError: null
        },
        form: null
      } as never
    }).body;
    expect(noneReady).toContain('No Telegram account is ready.');
    expect(noneReady).toContain('href="/admin/telegram"');
    expect(noneReady).toContain('<button type="submit" disabled=""');
  });

  it('shows selected suggestion context with a cancel path while retaining the atomic suggestion id', () => {
    const { body } = render(AddSourceByReference, {
      props: {
        telegramAccounts: [telegramAccount()],
        initialReference: 'https://t.me/telegram_source',
        initialSuggestionId: 'suggestion-telegram'
      }
    });

    expect(body).toContain('<strong>Selected suggestion:</strong> https://t.me/telegram_source');
    expect(body).toContain('name="suggestion_id" value="suggestion-telegram"');
    expect(body).toContain('name="reference" placeholder="@public_channel" required="" value="https://t.me/telegram_source"');
    expect(body).toContain('Cancel suggestion');
    expect(body).toContain('This source and its matching suggestion will be saved together.');
  });
});

function telegramSuggestion(): ChannelSuggestionRead {
  return suggestion({ id: 'suggestion-telegram', platform: 'telegram', channel_url: 'https://t.me/telegram_source' });
}

function suggestion(overrides: Partial<ChannelSuggestionRead> = {}): ChannelSuggestionRead {
  return {
    id: 'suggestion-default',
    user_id: '99999999-9999-4999-8999-999999999999',
    platform: 'telegram',
    channel_url: 'https://t.me/default_source',
    status: 'pending',
    admin_note: null,
    reviewed_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}

function sourceChannel(overrides: Partial<AdminSourceChannelRead> = {}): AdminSourceChannelRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    platform: 'telegram',
    platform_id: '-1001234',
    username: 'source_handle',
    title: 'Source title',
    subscriber_count: 100,
    is_active: true,
    is_paused: false,
    catchup_enabled: true,
    live_enabled: true,
    engagement_enabled: true,
    catchup_message_limit: 500,
    telegram_session_id: '22222222-2222-4222-8222-222222222222',
    telegram_session_name: 'Primary ingest',
    is_orphaned: false,
    is_indexable: true,
    last_read_post_id: '99',
    last_fetched_at: '2026-01-01T00:00:00Z',
    operational_status: 'active',
    freshness_status: 'fresh',
    seconds_since_last_fetch: 300,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}

function telegramAccount(overrides: Partial<AdminTelegramSessionRead> = {}): AdminTelegramSessionRead {
  return {
    id: '22222222-2222-4222-8222-222222222222',
    name: 'primary',
    display_name: 'Primary ingest',
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
    account_user_id: 1,
    account_username: 'primary',
    account_phone_hint: 'ending-1234',
    has_string_session: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}
