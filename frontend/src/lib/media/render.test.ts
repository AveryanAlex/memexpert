import { describe, expect, it } from 'vitest';

import type { PublicMemeFileRead } from '$lib/api/types';
import { selectMediaRender } from './render';

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
