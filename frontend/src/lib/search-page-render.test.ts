import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, MemeResultAttributionRead, PublicMemeCardRead, PublicMemeSearchPageRead, WebCollectionListRead } from '$lib/api/types';
import { parseSearchParams } from '$lib/searchParams';
import SearchPage from '../routes/search/+page.svelte';

describe('/search page', () => {
  it('renders compact consumer controls and native collection values while the filter drawer is closed', () => {
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

    const text = visibleText(body);

    expect(text).toContain('Search memes');
    expect(text).toContain('Filters');
    expect(text).toContain('Specific collections');
    expect(text).toContain('Team saves');
    expect(text).toContain('Browsing selected collections');
    expect(text).not.toContain('Search workspace');
    expect(text).not.toContain('URL-backed filter workspace');
    expect(text).not.toContain('collection_ids');
    expect(body).toMatch(/<form[^>]+method="GET"[^>]+action="\/search"[^>]*>/);
    expect(body).toMatch(/<input[^>]+type="hidden"[^>]+name="collection_ids"[^>]+value="team-id"[^>]*>/);
    expect(body).not.toContain('data-dialog-content');
    expect(body.match(/<input(?=[^>]*name="q")(?=[^>]*type="search")[^>]*>/g)).toHaveLength(1);
    const removeCollectionHref = body.match(/href="([^"]+)" aria-label="Remove Team saves filter"/)?.[1]?.replaceAll('&amp;', '&');
    expect(removeCollectionHref).toContain('scope=public');
    expect(removeCollectionHref).not.toContain('collection_ids');
  });

  it('renders removable named collection chips and consumer-safe fallback labels', () => {
    const { body } = render(SearchPage, {
      props: {
        data: {
          session: sessionPayload(),
          sessionError: null,
          page: emptyPage(),
          collections: collectionList(),
          filters: parseSearchParams(
            new URLSearchParams(
              'q=missing&tags=reaction&include_nsfw=true&media_type=gif&language=en&scope=collections&collection_ids=team-id&collection_ids=archived-id'
            )
          ),
          seo: { canonicalUrl: 'https://memexpert.test/search', noindex: true },
          errorMessage: null,
          collectionErrorMessage: null
        }
      }
    });

    const text = visibleText(body);

    expect(text).toContain('Results for “missing”');
    expect(text).toContain('#reaction ×');
    expect(body).toContain('aria-label="Remove reaction filter"');
    expect(text).toContain('GIFs');
    expect(text).toContain('English');
    expect(text).toContain('Sensitive content');
    expect(text).toContain('Team saves');
    expect(body).toContain('aria-label="Remove Team saves filter"');
    expect(text).toContain('Selected collection');
    expect(text).not.toContain('2 selected from the current URL');
    expect(text).not.toContain('collection_ids');
    expect(body).toContain('collection_ids=archived-id');
    expect(body).not.toContain('collection%5Fids');
    expect(text).toContain('No memes found');
    expect(text).toContain('Try a shorter phrase, remove a tag, or broaden media and language filters.');
    expect(text).toContain('Browse everything');
  });

  it('keeps the closed drawer out of SSR output while showing search ideas', () => {
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

    const text = visibleText(body);

    expect(text).toContain('Browsing public memes');
    expect(text).toContain('Try a search');
    expect(text).toContain('Cat reactions');
    expect(text).toContain('Browse categories');
    expect(body).toContain('href="/search?q=cat+reaction&amp;');
    expect(body).toContain('href="/search?tags=reaction&amp;');
    expect(body).toContain('focus-visible:outline-accent');
    expect(text).not.toContain('Recent searches');
    expect(text).toContain('No memes found');
    expect(text).not.toContain('Discovery filters');
    expect(body).not.toContain('data-dialog-content');
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

    const text = visibleText(body);

    expect(text).toContain('Results for “vault”');
    expect(text).toContain('Specific collections');
    expect(text).toContain('Sign in with access to this collection to search it.');
    expect(text).toContain('Retry');
    expect(text).not.toContain('Browse everything');
  });

  it('renders populated results in backend order through the hydration-stable masonry fallback', () => {
    const { body } = render(SearchPage, {
      props: {
        data: {
          session: null,
          sessionError: null,
          page: populatedPage(),
          collections: null,
          filters: parseSearchParams(new URLSearchParams('q=ranked')),
          seo: { canonicalUrl: 'https://memexpert.test/search?q=ranked', noindex: true },
          errorMessage: null,
          collectionErrorMessage: null
        }
      }
    });

    expect(body).toContain('data-layout="masonry"');
    expect(body).toContain('data-masonry-state="pending"');
    expect(body.indexOf('First ranked result')).toBeLessThan(body.indexOf('Second ranked result'));
    expect(body.indexOf('Second ranked result')).toBeLessThan(body.indexOf('Third ranked result'));
    expect(body).toContain('aria-posinset="1"');
    expect(body).toContain('aria-setsize="3"');
  });
});

function visibleText(body: string): string {
  return body
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<[^>]*>/g, ' ')
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

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

function populatedPage(): PublicMemeSearchPageRead {
  const captions = ['First ranked result', 'Second ranked result', 'Third ranked result'];
  return {
    items: captions.map((caption, index) => ({
      meme: searchMeme(`rank-${index + 1}`, caption),
      attribution: searchAttribution(index + 1)
    })),
    limit: 12,
    offset: 0,
    total: captions.length,
    has_more: false,
    request_id: 'req_ranked'
  };
}

function searchMeme(id: string, caption: string): PublicMemeCardRead {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 0,
    tags: ['ranked'],
    primary_file: null,
    caption,
    seo_page_slug: null,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function searchAttribution(rank: number): MemeResultAttributionRead {
  return {
    request_id: 'req_ranked',
    impression_id: `imp_rank_${rank}`,
    surface: 'web_search',
    source_algorithm: 'search',
    rank,
    query: 'ranked',
    filters: {
      language: null,
      media_type: null,
      include_nsfw: false,
      tags: [],
      scope: 'public',
      collection_ids: []
    },
    collection_scope: null,
    collection_ids: [],
    source_meme_id: null,
    algorithm_version: 'test-v1',
    score: null,
    score_components: {},
    reason: null
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
