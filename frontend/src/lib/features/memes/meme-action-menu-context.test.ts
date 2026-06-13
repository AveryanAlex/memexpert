import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, PublicMemeCardRead } from '$lib/api/types';
import { viewerCapabilitiesContextKey, viewerCapabilitiesFromSession } from '$lib/viewer-capabilities';
import MemeActionMenu from './MemeActionMenu.svelte';
import MemeGrid from './MemeGrid.svelte';

describe('MemeActionMenu viewer capabilities', () => {
  it('renders grid cards under full-account context without account props', () => {
    const { body } = render(MemeGrid, {
      props: {
        memes: [memeCard('11111111-1111-4111-8111-111111111111', 'Context pin meme')]
      },
      context: viewerContext(true)
    });

    expect(body).toContain('Actions for Context pin meme');
    expect(body).not.toContain('Pin requires a full account');
  });

  it('shows the primary Pin action for full accounts', () => {
    const { body } = render(MemeActionMenu, {
      props: {
        meme: memeCard('33333333-3333-4333-8333-333333333333', 'Full pin meme'),
        showPrimary: true
      },
      context: viewerContext(true)
    });

    expect(body).toContain('Like (4)');
    expect(body).toContain('Pin');
    expect(body).not.toContain('Pin requires a full account');
  });

  it('shows the primary Pin restriction for guest and non-full accounts', () => {
    const { body } = render(MemeActionMenu, {
      props: {
        meme: memeCard('22222222-2222-4222-8222-222222222222', 'Guest pin meme'),
        showPrimary: true
      },
      context: viewerContext(false)
    });

    expect(body).toContain('Like (4)');
    expect(body).toContain('Pin requires a full account');
    expect(body).not.toContain('>Pin</button>');
  });
});

function viewerContext(fullAccount: boolean): Map<unknown, unknown> {
  return new Map([[viewerCapabilitiesContextKey, () => viewerCapabilitiesFromSession(fullAccount ? fullSession() : guestSession())]]);
}

function fullSession(): CurrentSessionRead {
  return {
    user: {
      id: 'full-user-id',
      account_type: 'full' as const,
      telegram_id: 123,
      google_id: null,
      email: 'user@example.com',
      email_verified_at: null,
      language: 'en',
      nsfw_enabled: false,
      token_nonce: 1,
      status: 'active' as const,
      guest_expires_at: null,
      active_save_collection_id: 'favorites',
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: 'user@example.com',
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: true
    }
  };
}

function guestSession(): CurrentSessionRead {
  return {
    user: {
      id: 'guest-user-id',
      account_type: 'guest' as const,
      telegram_id: null,
      google_id: null,
      email: null,
      email_verified_at: null,
      language: 'en',
      nsfw_enabled: false,
      token_nonce: 1,
      status: 'active' as const,
      guest_expires_at: '2026-07-12T00:00:00Z',
      active_save_collection_id: 'favorites',
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: false
    }
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
