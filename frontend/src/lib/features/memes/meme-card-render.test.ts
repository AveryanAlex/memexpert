import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead } from '$lib/api/types';
import MemeCard from './MemeCard.svelte';
import MemeGrid from './MemeGrid.svelte';

describe('MemeCard', () => {
  it('renders direct Favorite, Save, and Send actions without a dense metadata footer', () => {
    const { body } = render(MemeCard, { props: { meme: memeCard() } });

    expect(body).toContain('Favorite');
    expect(body).toContain('Save');
    expect(body).toContain('Send');
    expect(body).not.toContain('640x360');
    expect(body).not.toContain('>en</');
    expect(body).not.toContain('#reaction');
  });

  it('only renders shared/private visibility badges when explicitly enabled', () => {
    const hiddenShared = render(MemeCard, { props: { meme: memeCard({ viewer_access: { visibility: 'shared' } }) } });
    const shared = render(MemeCard, { props: { meme: memeCard({ viewer_access: { visibility: 'shared' } }), showAccessMarkers: true } });
    const privateCard = render(MemeCard, { props: { meme: memeCard({ viewer_access: { visibility: 'private' } }), showAccessMarkers: true } });
    const publicCard = render(MemeCard, { props: { meme: memeCard(), showAccessMarkers: true } });

    expect(hiddenShared.body).not.toContain('Shared');
    expect(shared.body).toContain('Shared');
    expect(privateCard.body).toContain('Private');
    expect(publicCard.body).not.toContain('Shared');
    expect(publicCard.body).not.toContain('Private');
  });

  it('keeps backend result order in the ordered search layout', () => {
    const memes = [
      memeCard({ id: 'first', caption: 'First result' }),
      memeCard({ id: 'second', caption: 'Second result' }),
      memeCard({ id: 'third', caption: 'Third result' })
    ];
    const { body } = render(MemeGrid, { props: { memes, layout: 'ordered' } });

    expect(body).toContain('data-layout="ordered"');
    expect(body.indexOf('First result')).toBeLessThan(body.indexOf('Second result'));
    expect(body.indexOf('Second result')).toBeLessThan(body.indexOf('Third result'));
  });
});

function memeCard(overrides: Partial<PublicMemeCardRead> = {}): PublicMemeCardRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 0,
    tags: ['reaction'],
    primary_file: {
      id: '11111111-1111-4111-8111-111111111111-file',
      mime_type: 'image/jpeg',
      width: 640,
      height: 360,
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
        height: 360,
        blur_hash: null
      }
    },
    caption: 'Launch reaction',
    seo_page_slug: null,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}
