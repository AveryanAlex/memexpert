import { describe, expect, it } from 'vitest';

import type { PublicMemeFileRead } from '$lib/api/types';
import {
  FEED_PREVIEW_FALLBACK_ASPECT_RATIO,
  selectFeedPreviewAspectRatio,
  selectImageLoading,
  selectMediaAspectRatio,
  selectMediaPreload,
  selectMediaRender,
  selectMediaZoomImage,
  selectVideoSourceType
} from './render';

describe('selectMediaRender', () => {
  it('selects real image display and download URLs', () => {
    const media = selectMediaRender(file({ display_url: 'https://img.example/display.webp', download_url: 'https://img.example/download.jpg' }));

    expect(media).toEqual({
      imageUrl: 'https://img.example/display.webp',
      videoUrl: null,
      audioUrl: null,
      downloadUrl: 'https://img.example/download.jpg',
      hasMedia: true
    });
  });

  it('selects web video URLs without requiring an image', () => {
    const media = selectMediaRender(file({ web_video_url: 'https://media.example/file.mp4' }));

    expect(media.videoUrl).toBe('https://media.example/file.mp4');
    expect(media.imageUrl).toBeNull();
    expect(media.hasMedia).toBe(true);
  });

  it('selects authenticated private collection/profile render variants', () => {
    const media = selectMediaRender(
      file({
        preview_url: '/api/v1/media/files/file-1/preview',
        download_url: '/api/v1/media/files/file-1/download',
        web_video_url: '/api/v1/media/files/file-1/web-video.mp4'
      })
    );

    expect(media.imageUrl).toBe('/api/v1/media/files/file-1/preview');
    expect(media.videoUrl).toBe('/api/v1/media/files/file-1/web-video.mp4');
    expect(media.downloadUrl).toBe('/api/v1/media/files/file-1/download');
    expect(media.hasMedia).toBe(true);
  });

  it('selects authenticated audio originals as audio instead of images', () => {
    const media = selectMediaRender({
      ...file({ original_url: '/api/v1/media/files/file-1/original' }),
      mime_type: 'audio/mpeg'
    });

    expect(media.audioUrl).toBe('/api/v1/media/files/file-1/original');
    expect(media.imageUrl).toBeNull();
    expect(media.hasMedia).toBe(true);
  });

  it('falls back when render URLs are absent', () => {
    expect(selectMediaRender(null)).toEqual({
      imageUrl: null,
      videoUrl: null,
      audioUrl: null,
      downloadUrl: null,
      hasMedia: false
    });
  });
});

describe('selectMediaZoomImage', () => {
  it('prefers the original image and falls back through display variants', () => {
    expect(
      selectMediaZoomImage(
        file({
          original_url: 'https://img.example/original.jpg',
          display_url: 'https://img.example/display.webp',
          preview_url: 'https://img.example/preview.webp'
        })
      )
    ).toBe('https://img.example/original.jpg');
    expect(selectMediaZoomImage(file({ display_url: 'https://img.example/display.webp' }))).toBe('https://img.example/display.webp');
  });

  it('does not expose audio or video originals as zoomable images', () => {
    const original = { original_url: '/api/v1/media/files/file-1/original' };

    expect(selectMediaZoomImage({ ...file(original), mime_type: 'audio/mpeg' })).toBeNull();
    expect(selectMediaZoomImage({ ...file(original), mime_type: 'video/mp4' })).toBeNull();
  });
});

describe('selectMediaAspectRatio', () => {
  it('prefers render dimensions over source file dimensions', () => {
    expect(selectMediaAspectRatio(file({ width: 320, height: 180 }))).toBe('320 / 180');
  });

  it('falls back to source file dimensions', () => {
    expect(selectMediaAspectRatio({ ...file({ width: null, height: null }), width: 1024, height: 768 })).toBe('1024 / 768');
  });

  it('returns null when dimensions are unavailable or invalid', () => {
    expect(selectMediaAspectRatio(null)).toBeNull();
    expect(selectMediaAspectRatio({ ...file({ width: 0, height: 180 }), width: null, height: null })).toBeNull();
  });
});

describe('feed preview media loading', () => {
  it('uses known dimensions to reserve mixed media layout space', () => {
    expect(selectFeedPreviewAspectRatio(file({ width: 320, height: 180 }))).toBe('320 / 180');
  });

  it('uses a stable fallback aspect ratio when feed media dimensions are missing', () => {
    expect(selectFeedPreviewAspectRatio(null)).toBe(FEED_PREVIEW_FALLBACK_ASPECT_RATIO);
    expect(selectFeedPreviewAspectRatio({ ...file({ width: null, height: null }), width: null, height: null })).toBe(
      FEED_PREVIEW_FALLBACK_ASPECT_RATIO
    );
  });

  it('keeps feed images lazy and feed video/audio metadata deferred', () => {
    expect(selectImageLoading(false)).toBe('lazy');
    expect(selectImageLoading(true)).toBe('eager');
    expect(selectMediaPreload(false)).toBe('none');
    expect(selectMediaPreload(true)).toBe('metadata');
  });
});

describe('selectVideoSourceType', () => {
  it('labels generated web video variants as MP4 instead of the original MIME type', () => {
    expect(selectVideoSourceType({ ...file({ web_video_url: '/api/v1/media/files/file-1/web-video.mp4' }), mime_type: 'video/quicktime' })).toBe('video/mp4');
    expect(selectVideoSourceType({ ...file({ original_url: '/api/v1/media/files/file-1/original' }), mime_type: 'video/quicktime' })).toBe('video/quicktime');
  });
});

function file(render: Partial<NonNullable<PublicMemeFileRead['render']>>): PublicMemeFileRead {
  return {
    id: 'file-1',
    mime_type: 'image/jpeg',
    width: 640,
    height: 480,
    file_size_bytes: null,
    blur_hash: null,
    quality_score: 0.8,
    render: {
      thumbnail_url: null,
      preview_url: null,
      display_url: null,
      original_url: null,
      download_url: null,
      web_video_url: null,
      width: 640,
      height: 480,
      blur_hash: null,
      ...render
    }
  };
}
