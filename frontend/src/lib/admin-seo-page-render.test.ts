import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AdminMemeSeoReviewRowRead } from '$lib/api/types';
import SeoPage from '../routes/admin/content/seo/+page.svelte';

describe('/admin/content/seo page', () => {
  it('renders a status-first SEO queue with editors and overwrite controls collapsed per row', () => {
    const missing = review({ meme: meme('11111111-1111-4111-8111-111111111111'), status: 'missing', seo_page: null });
    const generated = review({
      meme: meme('22222222-2222-4222-8222-222222222222', { media_type: 'video', tags: ['launch', 'reaction'] }),
      status: 'generated',
      seo_page: page('22222222-2222-4222-8222-222222222222', { edited_at: null })
    });
    const edited = review({
      meme: meme('33333333-3333-4333-8333-333333333333'),
      status: 'edited',
      seo_page: page('33333333-3333-4333-8333-333333333333', { page_title: 'Edited launch reaction', edited_at: '2026-07-10T23:45:30-04:00' })
    });
    const { body } = render(SeoPage, {
      props: {
        data: {
          reviews: [missing, generated, edited],
          paging: { page: 2, pageSize: 25, hasPrevious: true, hasNext: true },
          loadError: null
        },
        form: null
      } as never
    });

    expect(body).toContain('SEO review queue');
    expect(body).toContain('Needs SEO');
    expect(body).toContain('Generated');
    expect(body).toContain('Manually edited');
    expect(body).toContain('No SEO page has been created for this meme yet.');
    expect(body).toContain('A launch reaction meme for search.');
    expect(body).toContain('data-admin-media-preview');
    expect(body).toContain(`href="/admin/memes/${missing.meme.id}"`);
    expect(body).toContain('Review meme');
    expect(body).toContain('SEO pages to review');
    expect(body).toContain('Edit SEO details');
    expect(body).toContain('Regenerate and overwrite SEO');
    expect(body).toContain('Type REGENERATE to confirm');
    expect(body).toContain("Generated output can replace SEO text and catalog tags, reassign this meme's template, and create an uncurated template. Manual edits are cleared.");
    expect(body).toContain('action="?page=2&amp;/updateSeoPage"');
    expect(body).toContain('action="?page=2&amp;/regenerateSeoPage"');
    expect(body).toContain('SEO tags (also updates catalog tags)');
    expect(body).toContain('Saving SEO tags also updates the catalog tags for this meme.');
    expect(body).toMatch(/name="tags"[^>]*value="reaction"/);
    expect(body.match(/name="(?:slug|page_title|meta_description|alt_text)"[^>]*required/g)).toHaveLength(12);
    expect(body.match(/maxlength="255"/g)).toHaveLength(6);
    expect(body).toContain('2026-07-11 03:45 UTC');
    expect(body).toContain('aria-label="SEO review pagination"');
    expect(body).toContain('href="/admin/content/seo?page=1"');
    expect(body).toContain('href="/admin/content/seo?page=3"');
    expect(body).toContain('Page 2');
    expect(body).not.toContain(`${missing.meme.id} to confirm`);
    expect(body).not.toContain('Paste the meme ID');
    expect(body).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
  });

  it('renders action and load errors without a successful empty state', () => {
    const { body } = render(SeoPage, {
      props: {
        data: {
          reviews: [],
          paging: { page: 1, pageSize: 25, hasPrevious: false, hasNext: false },
          loadError: 'Could not load the SEO review queue.'
        },
        form: { message: 'Type REGENERATE to confirm this action.', error: true }
      } as never
    });

    expect(body.match(/role="alert"/g)).toHaveLength(2);
    expect(body).toContain('Could not load the SEO review queue.');
    expect(body).toContain('Type REGENERATE to confirm this action.');
    expect(body).not.toContain('No SEO pages need review');
    expect(body).not.toContain('No SEO pages on this page');
    expect(body).not.toContain('SEO review pagination');
  });

  it('distinguishes a later empty page from an empty global queue and retains Previous navigation', () => {
    const { body } = render(SeoPage, {
      props: {
        data: {
          reviews: [],
          paging: { page: 2, pageSize: 25, hasPrevious: true, hasNext: false },
          loadError: null
        },
        form: null
      } as never
    });

    expect(body).toContain('No SEO pages on this page');
    expect(body).toContain('There are no items on this page.');
    expect(body).toContain('href="/admin/content/seo?page=1"');
    expect(body).not.toContain('No SEO pages need review');
  });
});

function meme(id: string, overrides: Partial<AdminMemeSeoReviewRowRead['meme']> = {}): AdminMemeSeoReviewRowRead['meme'] {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    visibility_mode: 'auto',
    is_public: true,
    popularity_score: 4.2,
    like_count: 12,
    tags: ['reaction'],
    primary_file: null,
    template_id: null,
    created_at: '2026-07-10T10:00:00Z',
    updated_at: '2026-07-10T11:00:00Z',
    ...overrides
  };
}

function page(memeId: string, overrides: Partial<NonNullable<AdminMemeSeoReviewRowRead['seo_page']>> = {}): NonNullable<AdminMemeSeoReviewRowRead['seo_page']> {
  return {
    meme_id: memeId,
    slug: 'launch-reaction',
    page_title: 'Launch reaction meme',
    meta_description: 'A launch reaction meme for search.',
    alt_text: 'A reaction to launch day.',
    caption: 'Launch day mood',
    body_text: 'Longer search copy.',
    tags: ['launch', 'reaction'],
    model_id: 'seo-v1',
    prompt_version: 'prompt-v1',
    generated_at: '2026-07-10T11:00:00Z',
    edited_at: '2026-07-10T12:00:00Z',
    ...overrides
  };
}

function review(overrides: Partial<AdminMemeSeoReviewRowRead> & Pick<AdminMemeSeoReviewRowRead, 'meme' | 'seo_page' | 'status'>): AdminMemeSeoReviewRowRead {
  return overrides;
}
