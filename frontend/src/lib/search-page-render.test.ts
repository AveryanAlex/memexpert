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
          errorMessage: null,
          collectionErrorMessage: null
        }
      }
    });

    expect(body).toContain('Search scope');
    expect(body).toContain('Specific collections');
    expect(body).toContain('My private saves');
    expect(body).toContain('All I can access');
    expect(body).toContain('Search only memes anyone can open.');
    expect(body).toContain('Search only memes saved in the readable collections selected below.');
    expect(body).toContain('collection_ids');
    expect(body).toContain('Team saves');
    expect(body).toContain('1 selected from the current URL');
    expect(body).toContain('Browsing specific collections');
  });

  it('renders compact collection filter degraded states', () => {
    const noSession = render(SearchPage, {
      props: {
        data: {
          session: null,
          sessionError: null,
          page: emptyPage(),
          collections: null,
          filters: parseSearchParams(new URLSearchParams('scope=collections')),
          seo: { canonicalUrl: 'https://memexpert.test/search', noindex: false },
          errorMessage: null,
          collectionErrorMessage: null
        }
      }
    });
    const loadError = render(SearchPage, {
      props: {
        data: {
          session: sessionPayload(),
          sessionError: null,
          page: emptyPage(),
          collections: null,
          filters: parseSearchParams(new URLSearchParams('scope=collections')),
          seo: { canonicalUrl: 'https://memexpert.test/search', noindex: false },
          errorMessage: null,
          collectionErrorMessage: 'Forbidden collection list.'
        }
      }
    });

    expect(noSession.body).toContain('Sign in to load collection choices. Public search remains available.');
    expect(loadError.body).toContain('Forbidden collection list.');
    expect(loadError.body).toContain('Collection choices could not load; text, tag, scope, media, language, and NSFW filters still work.');
  });

  it('renders selected collection labels, URL fallback summaries, and empty actions', () => {
    const { body } = render(SearchPage, {
      props: {
        data: {
          session: sessionPayload(),
          sessionError: null,
          page: emptyPage(),
          collections: collectionList(),
          filters: parseSearchParams(new URLSearchParams('q=missing&scope=collections&collection_ids=team-id&collection_ids=archived-id')),
          seo: { canonicalUrl: 'https://memexpert.test/search', noindex: true },
          errorMessage: null,
          collectionErrorMessage: null
        }
      }
    });

    expect(body).toContain('Results for “missing”');
    expect(body).toContain('Team saves');
    expect(body).toContain('2 selected from the current URL');
    expect(body).toContain('1 selected collection');
    expect(body).toContain('No memes found');
    expect(body).toContain('Try a shorter phrase, remove a tag, or broaden media and language filters.');
    expect(body).toContain('Browse everything');
  });

  it('keeps collection controls enabled in the default public state for native form submission', () => {
    const { body } = render(SearchPage, {
      props: {
        data: {
          session: sessionPayload(),
          sessionError: null,
          page: emptyPage(),
          collections: collectionList(),
          filters: parseSearchParams(new URLSearchParams()),
          seo: { canonicalUrl: 'https://memexpert.test/search', noindex: false },
          errorMessage: null,
          collectionErrorMessage: null
        }
      }
    });

    const fieldset = body.match(/<fieldset[^>]+aria-describedby="collection-filter-help collection-filter-state"[^>]*>/)?.[0] ?? '';
    const collectionInput = body.match(/<input[^>]+name="collection_ids"[^>]+value="team-id"[^>]*>/)?.[0] ?? '';

    expect(body).toContain('Browsing public catalog');
    expect(body).toContain('Choose Specific collections above to enable collection filters.');
    expect(body).toContain('Team saves');
    expect(fieldset).not.toContain('disabled');
    expect(collectionInput).toContain('name="collection_ids"');
    expect(collectionInput).not.toContain('disabled');
    expect(body).toContain('No memes found');
    expect(body).not.toContain('Clear all');
  });

  it('renders a compact search error state without empty-state actions', () => {
    const { body } = render(SearchPage, {
      props: {
        data: {
          session: null,
          sessionError: 'Smoke test runs as a guest browser.',
          page: emptyPage(),
          collections: null,
          filters: parseSearchParams(new URLSearchParams('q=vault&scope=collections&collection_ids=team-id')),
          seo: { canonicalUrl: 'https://memexpert.test/search', noindex: true },
          errorMessage: 'Sign in with access to this collection to search it.',
          collectionErrorMessage: null
        }
      }
    });

    expect(body).toContain('Results for “vault”');
    expect(body).toContain('Specific collections');
    expect(body).toContain('Sign in with access to this collection to search it.');
    expect(body).toContain('Retry');
    expect(body).not.toContain('Browse everything');
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
