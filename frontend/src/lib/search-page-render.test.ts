import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, PublicMemeSearchPageRead, WebCollectionListRead } from '$lib/api/types';
import { parseSearchParams } from '$lib/searchParams';
import SearchPage from '../routes/search/+page.svelte';

describe('/search page', () => {
  it('renders URL-backed scope and collection search controls', () => {
    const { body } = render(SearchPage, {
      props: {
        data: {
          session: sessionPayload(),
          sessionError: null,
          page: emptyPage(),
          collections: collectionList(),
          filters: parseSearchParams(new URLSearchParams('scope=collections&collection_ids=team-id')),
          seo: { canonicalUrl: 'https://memexpert.test/search', noindex: false },
          errorMessage: null
        }
      }
    });

    expect(body).toContain('Search scope');
    expect(body).toContain('Specific collections');
    expect(body).toContain('My private saves');
    expect(body).toContain('All I can access');
    expect(body).toContain('Team saves');
    expect(body).toContain('Browsing specific collections');
  });
});

function emptyPage(): PublicMemeSearchPageRead {
  return {
    items: [],
    limit: 12,
    offset: 0,
    total: 0,
    has_more: false,
    request_id: 'req_empty'
  };
}

function collectionList(): WebCollectionListRead {
  const collection = {
    id: 'team-id',
    owner_id: 'user-id',
    title: 'Team saves',
    description: null,
    kind: 'custom' as const,
    visibility: 'private' as const,
    memberships: [],
    invites: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };

  return {
    active_save_collection_id: collection.id,
    collections: [
      {
        collection,
        viewer_role: 'editor',
        capabilities: {
          can_view: true,
          can_add_memes: true,
          can_remove_memes: true,
          can_rename: false,
          can_delete: false,
          can_create_invites: true,
          can_revoke_invites: true,
          can_manage_members: false,
          can_set_active_save: true
        },
        active_save_collection_id: collection.id
      }
    ]
  };
}

function sessionPayload(): CurrentSessionRead {
  return {
    user: {
      id: 'user-id',
      account_type: 'full',
      telegram_id: 123,
      google_id: null,
      email: null,
      email_verified_at: null,
      language: 'any',
      nsfw_enabled: false,
      token_nonce: 0,
      status: 'active',
      guest_expires_at: null,
      active_save_collection_id: null,
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: true
    }
  };
}
