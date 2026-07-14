import type { PublicMemeFileRead } from '$lib/api/types';

export interface SelectedMediaRender {
  imageUrl: string | null;
  videoUrl: string | null;
  audioUrl: string | null;
  downloadUrl: string | null;
  hasMedia: boolean;
}

export const FEED_PREVIEW_FALLBACK_ASPECT_RATIO = '4 / 3';

interface MediaDimensions {
  width: number;
  height: number;
}

export function selectMediaRender(file: PublicMemeFileRead | null | undefined): SelectedMediaRender {
  const render = file?.render;
  const originalUrl = render?.original_url ?? file?.render_url ?? null;
  const isAudio = file?.mime_type?.startsWith('audio/') ?? false;
  const isVideo = file?.mime_type?.startsWith('video/') ?? false;
  const videoUrl = render?.web_video_url ?? (isVideo ? originalUrl : null);
  const imageUrl = isAudio
    ? null
    : render?.display_url ?? render?.preview_url ?? render?.thumbnail_url ?? (isVideo ? null : originalUrl);
  const audioUrl = isAudio ? originalUrl : null;
  const downloadUrl = render?.download_url ?? file?.download_url ?? null;

  return {
    imageUrl,
    videoUrl,
    audioUrl,
    downloadUrl,
    hasMedia: Boolean(videoUrl || imageUrl || audioUrl)
  };
}

export function selectMediaZoomImage(file: PublicMemeFileRead | null | undefined): string | null {
  const mimeType = file?.mime_type?.toLowerCase() ?? '';
  if (mimeType.startsWith('audio/') || mimeType.startsWith('video/')) return null;

  const render = file?.render;
  return render?.original_url ?? file?.render_url ?? render?.display_url ?? render?.preview_url ?? render?.thumbnail_url ?? null;
}

export function selectMediaAspectRatio(file: PublicMemeFileRead | null | undefined): string | null {
  const dimensions = selectMediaDimensions(file);
  return dimensions ? `${dimensions.width} / ${dimensions.height}` : null;
}

export function selectFeedPreviewAspectRatio(file: PublicMemeFileRead | null | undefined): string {
  return selectMediaAspectRatio(file) ?? FEED_PREVIEW_FALLBACK_ASPECT_RATIO;
}

export function selectImageLoading(detail: boolean): 'eager' | 'lazy' {
  return detail ? 'eager' : 'lazy';
}

export function selectMediaPreload(detail: boolean): 'metadata' | 'none' {
  return detail ? 'metadata' : 'none';
}

export function selectVideoSourceType(file: PublicMemeFileRead | null | undefined): string {
  return file?.render?.web_video_url ? 'video/mp4' : file?.mime_type || 'video/mp4';
}

function selectMediaDimensions(file: PublicMemeFileRead | null | undefined): MediaDimensions | null {
  const renderDimensions = validDimensions(file?.render?.width, file?.render?.height);
  if (renderDimensions) return renderDimensions;

  return validDimensions(file?.width, file?.height);
}

function validDimensions(width: number | null | undefined, height: number | null | undefined): MediaDimensions | null {
  if (!width || !height || width <= 0 || height <= 0) return null;
  return { width, height };
}
