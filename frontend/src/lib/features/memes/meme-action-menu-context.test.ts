import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, PublicMemeCardRead } from '$lib/api/types';
import { createMemeActionState, memeActionStateContextKey } from '$lib/meme-action-state';
import { viewerCapabilitiesContextKey, viewerCapabilitiesFromSession } from '$lib/viewer-capabilities';
import MemeActionMenu from './MemeActionMenu.svelte';
import MemeGrid from './MemeGrid.svelte';

describe('MemeActionMenu viewer capabilities', () => {
  it('renders grid cards under full-account context without account props', () => {
    const { body } = render(MemeGrid, {
      props: {
        memes: [memeCard('11111111-1111-4111-8111-111111111111', 'Context pin meme')],
        bulk: { enabled: true }
      },
      context: viewerContext(true)
    });

    expect(body).toContain('Actions for Context pin meme');
    expect(body).toContain('aria-label="Favorite"');
    expect(body).toContain('aria-label="Download"');
    expect(body).toContain('aria-label="Save to collection"');
    expect(body).toContain('aria-label="Send"');
    expect(body).toContain('Select items');
    expect(body).not.toContain('Bulk actions');
    expect(body).not.toContain('type="checkbox"');
    expect(body).not.toContain('Pin requires a full account');
  });

  it('renders labeled detail actions and keeps the overflow available for full accounts', () => {
    const { body } = render(MemeActionMenu, {
      props: {
        meme: memeCard('33333333-3333-4333-8333-333333333333', 'Full pin meme'),
        surface: 'detail'
      },
      context: viewerContext(true)
    });

    expect(body).toContain('Favorite (4)');
    expect(body).toContain('aria-label="Save to collection"');
    expect(body).toContain('Send');
    expect(body).toContain('aria-label="Meme actions"');
    expect(body).not.toContain('Favorite meme');
    expect(body).not.toContain('Send to Telegram');
    expect(body).not.toContain('Pin requires a full account');
  });

  it('does not expose the Pin action to guest and non-full accounts', () => {
    const { body } = render(MemeActionMenu, {
      props: {
        meme: memeCard('22222222-2222-4222-8222-222222222222', 'Guest pin meme'),
        surface: 'detail'
      },
      context: viewerContext(false)
    });

    expect(body).toContain('Favorite (4)');
    expect(body).not.toContain('Pin requires a full account');
    expect(body).not.toContain('>Pin</');
  });

  it('renders shared Favorite, Save, and like-count patches', () => {
    const meme = memeCard('44444444-4444-4444-8444-444444444444', 'Shared action state meme');
    const actionState = createMemeActionState('full-user-id');
    actionState.publish(meme.id, { favorited: true, saved: true, likeCount: 5 });
    const context = viewerContext(true);
    context.set(memeActionStateContextKey, actionState);

    const { body } = render(MemeActionMenu, {
      props: { meme, surface: 'detail' },
      context
    });

    expect(body).toContain('Favorite (5)');
    expect(body).toContain('fill-current text-danger');
    expect(body).toContain('Saved');
    expect(body).toContain('fill-current text-accent');
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
