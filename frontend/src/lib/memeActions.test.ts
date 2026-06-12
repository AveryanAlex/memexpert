import { describe, expect, it } from 'vitest';

import { ApiError } from '$lib/api/client';
import type { PublicMemeDetailRead } from '$lib/api/types';
import {
  actionFailureMessage,
  canonicalMemeUrl,
  memeDownloadUrl,
  memeHref,
  memeRenderUrl,
  telegramShareUrl
} from './memeActions';

describe('meme action helpers', () => {
  it('builds canonical meme and Telegram share URLs', () => {
    const meme = detail({ id: 'meme-123', seo_page_slug: 'frog-wizard' });

    expect(memeHref(meme)).toBe('/memes/frog-wizard');
    expect(canonicalMemeUrl(meme, 'https://memexpert.test/app')).toBe('https://memexpert.test/memes/frog-wizard');
    expect(telegramShareUrl('https://memexpert.test/memes/frog-wizard', 'Frog wizard')).toBe(
      'https://t.me/share/url?url=https%3A%2F%2Fmemexpert.test%2Fmemes%2Ffrog-wizard&text=Frog+wizard'
    );
  });

  it('falls back through meme and file media URLs', () => {
    const meme = detail({
      id: 'meme-123',
      seo_page_slug: null,
      primary_file: {
        id: 'file-1',
        mime_type: 'image/png',
        width: 100,
        height: 100,
        file_size_bytes: 12,
        blur_hash: null,
        quality_score: 1,
        render: null,
        render_url: 'https://cdn.memexpert.test/render.png',
        download_url: 'https://cdn.memexpert.test/download.png'
      }
    });

    expect(memeRenderUrl(meme)).toBe('https://cdn.memexpert.test/render.png');
    expect(memeDownloadUrl(meme)).toBe('https://cdn.memexpert.test/download.png');
  });

  it('formats account and active collection failures clearly', () => {
    expect(actionFailureMessage('pin', new ApiError(403, 'Full account required.'))).toBe(
      'Pinning requires a full MemeXpert account. Link or sign in, then try again.'
    );
    expect(actionFailureMessage('save', new ApiError(409, 'Active collection is read-only.'))).toBe(
      'Could not update your active save collection: Active collection is read-only.'
    );
    expect(actionFailureMessage('report', new ApiError(403, 'A full account is required for this operation.'))).toBe(
      'A full account is required for this operation.'
    );
    expect(actionFailureMessage('report', new ApiError(404, 'Meme was not found.'))).toBe(
      'Could not submit report: Meme was not found.'
    );
    expect(actionFailureMessage('download', null)).toBe('Download is unavailable until this meme has a media download URL.');
  });
});

function detail(overrides: Partial<PublicMemeDetailRead> & { id: string; seo_page_slug: string | null }): PublicMemeDetailRead {
  const { id, seo_page_slug, ...rest } = overrides;

  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 0,
    tags: [],
    primary_file: null,
    caption: null,
    seo_page_slug,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ocr_text: null,
    seo_title: null,
    seo_description: null,
    seo_alt_text: null,
    seo_body_text: null,
    seo_model_id: null,
    seo_prompt_version: null,
    seo_generated_at: null,
    files: [],
    ...rest
  };
}
