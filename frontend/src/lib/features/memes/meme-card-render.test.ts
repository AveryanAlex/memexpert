import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead } from '$lib/api/types';
import MemeCard from './MemeCard.svelte';
import MemeGrid from './MemeGrid.svelte';

describe('MemeCard', () => {
  it('renders five evenly distributed icon actions without visible action labels or a dense metadata footer', () => {
    const { body } = render(MemeCard, { props: { meme: memeCard() } });

    expect(body).toContain('grid-cols-5');
    expect(body).toContain('aria-label="Favorite"');
    expect(body).toContain('aria-label="Download"');
    expect(body).toContain('aria-label="Save to collection"');
    expect(body).toContain('aria-label="Send"');
    expect(body).toContain('aria-label="Enlarge Launch reaction"');
    expect(body).toContain('!hidden !size-10');
    expect(body).toContain('min-[600px]:!grid');
    expect(body).not.toContain('<span>Favorite</span>');
    expect(body).not.toContain('<span>Save</span>');
    expect(body).not.toContain('<span>Send</span>');
    expect(body).not.toContain('640x360');
    expect(body).not.toContain('>en</');
    expect(body).not.toContain('#reaction');
  });

  it('only offers image enlargement for image media with a usable source', () => {
    const image = render(MemeCard, { props: { meme: memeCard() } });
    const base = memeCard();
    const video = render(MemeCard, {
      props: {
        meme: memeCard({
          media_type: 'video',
          primary_file: {
            ...base.primary_file!,
            mime_type: 'video/mp4',
            render: {
              ...base.primary_file!.render!,
              original_url: '/original.mp4',
              display_url: '/video-preview.webp',
              web_video_url: '/video.mp4'
            }
          }
        })
      }
    });

    expect(image.body).toContain('aria-label="Enlarge Launch reaction"');
    expect(video.body).not.toContain('aria-label="Enlarge Launch reaction"');
    expect(video.body).toContain('poster="/video-preview.webp"');
  });

  it('hides the fallback title row without leaving a dangling labelled-by reference', () => {
    const untitled = render(MemeCard, { props: { meme: memeCard({ caption: null, tags: [] }) } });
    const privateUntitled = render(MemeCard, {
      props: {
        meme: memeCard({ caption: null, tags: [], viewer_access: { visibility: 'private' } }),
        showAccessMarkers: true
      }
    });

    expect(untitled.body).not.toContain('aria-labelledby="meme-card-title-');
    expect(untitled.body).not.toContain('id="meme-card-title-');
    expect(privateUntitled.body).toContain('Private');
    expect(privateUntitled.body).not.toContain('id="meme-card-title-');
  });

  it('renders a filled red heart for an already-favorited meme', () => {
    const { body } = render(MemeCard, { props: { meme: memeCard({ viewer_has_favorited: true }) } });

    expect(body).toContain('aria-label="Remove favorite"');
    expect(body).toContain('fill-current text-danger');
    expect(body).not.toContain('Favorited');
  });

  it('renders a filled bookmark only for a meme saved outside Favorites', () => {
    const { body } = render(MemeCard, { props: { meme: memeCard({ viewer_has_saved: true }) } });

    expect(body).toMatch(/aria-label="Save to collection"[^>]*aria-pressed="true"/);
    expect(body).toContain('fill-current text-accent');
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
