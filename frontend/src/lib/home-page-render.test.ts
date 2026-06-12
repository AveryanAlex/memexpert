import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type {
  CurrentSessionRead,
  PublicMemeCardRead,
  PublicMemeSearchPageRead,
  WebCollectionListRead
} from '$lib/api/types';
import HomePage from '../routes/+page.svelte';

describe('/ page', () => {
  it('renders SSR feed results through the home infinite feed without page links', () => {
    const page: PublicMemeSearchPageRead = {
      items: [
        { meme: memeCard('11111111-1111-4111-8111-111111111111', 'SSR cat reaction') },
        { meme: memeCard('22222222-2222-4222-8222-222222222222', 'SSR launch mood') },
        { meme: videoMemeCard('33333333-3333-4333-8333-333333333333', 'SSR video mood') }
      ],
      limit: 3,
      offset: 0,
      total: 8,
      has_more: true
    };

    const { body } = render(HomePage, {
      props: {
        data: {
          session: fullSession(),
          sessionError: null,
          page,
          collections: collectionList(),
          query: 'cat',
          offset: 0,
          errorMessage: null
        },
        form: null
      }
    });

    expect(body).toContain('Find the right meme fast.');
    expect(body).toContain('action="/search"');
    expect(body).toContain('Your collections');
    expect(body).toContain('Favorites');
    expect(body).toContain('Results for');
    expect(body).toContain('SSR cat reaction');
    expect(body).toContain('SSR launch mood');
    expect(body).toContain('SSR video mood');
    expect(body).toContain('Showing 3 of 8');
    expect(body).toContain('Load more');
    expect(body).toContain('role="list"');
    expect(body).toContain('aria-posinset="1"');
    expect(body).toContain('loading="lazy"');
    expect(body).toContain('preload="none"');
    expect(body).toContain('Actions for SSR cat reaction');
    expect(body).not.toContain('Previous');
    expect(body).not.toContain('Next page');
  });
});

function fullSession(): CurrentSessionRead {
  return {
    user: {
      id: '33333333-3333-4333-8333-333333333333',
      account_type: 'full',
      telegram_id: null,
      google_id: null,
      email: 'user@example.com',
      email_verified_at: null,
      language: 'en',
      nsfw_enabled: false,
      token_nonce: 1,
      status: 'active',
      guest_expires_at: null,
      active_save_collection_id: '44444444-4444-4444-8444-444444444444',
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: 'user@example.com',
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: false
    }
  };
}

function collectionList(): WebCollectionListRead {
  const collection = {
    id: '44444444-4444-4444-8444-444444444444',
    owner_id: '33333333-3333-4333-8333-333333333333',
    title: 'Favorites',
    description: null,
    kind: 'favorites' as const,
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
        viewer_role: 'owner',
        capabilities: {
          can_view: true,
          can_add_memes: true,
          can_remove_memes: false,
          can_rename: false,
          can_delete: false,
          can_create_invites: false,
          can_set_active_save: true
        },
        active_save_collection_id: collection.id
      }
    ]
  };
}

function memeCard(id: string, caption: string): PublicMemeCardRead {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 10,
    like_count: 4,
    tags: ['reaction'],
    primary_file: {
      id: `${id}-file`,
      mime_type: 'image/jpeg',
      width: 640,
      height: 900,
      file_size_bytes: 1234,
      blur_hash: null,
      quality_score: 1,
      render: {
        thumbnail_url: '/thumb.jpg',
        preview_url: '/preview.jpg',
        display_url: '/display.jpg',
        original_url: '/original.jpg',
        download_url: '/download.jpg',
        web_video_url: null,
        width: 640,
        height: 900,
        blur_hash: null
      }
    },
    caption,
    seo_page_slug: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false
  };
}

function videoMemeCard(id: string, caption: string): PublicMemeCardRead {
  return {
    ...memeCard(id, caption),
    media_type: 'video',
    primary_file: {
      id: `${id}-file`,
      mime_type: 'video/mp4',
      width: null,
      height: null,
      file_size_bytes: 1234,
      blur_hash: null,
      quality_score: 1,
      render: {
        thumbnail_url: '/video-poster.jpg',
        preview_url: null,
        display_url: null,
        original_url: null,
        download_url: '/video-download.mp4',
        web_video_url: '/video.mp4',
        width: null,
        height: null,
        blur_hash: null
      }
    }
  };
}
