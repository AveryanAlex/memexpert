import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead } from '$lib/api/types';
import MemeCard from './MemeCard.svelte';

describe('MemeCard', () => {
  it('renders a shared/private visibility badge without labeling public cards', () => {
    const shared = render(MemeCard, { props: { meme: memeCard({ viewer_access: { visibility: 'shared' } }) } });
    const privateCard = render(MemeCard, { props: { meme: memeCard({ viewer_access: { visibility: 'private' } }) } });
    const publicCard = render(MemeCard, { props: { meme: memeCard() } });

    expect(shared.body).toContain('Shared');
    expect(privateCard.body).toContain('Private');
    expect(publicCard.body).not.toContain('Shared');
    expect(publicCard.body).not.toContain('Private');
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
    primary_file: null,
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
