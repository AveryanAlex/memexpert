import type { PublicMemeFileRead } from '$lib/api/types';

export interface SelectedMediaRender {
  imageUrl: string | null;
  videoUrl: string | null;
  audioUrl: string | null;
  downloadUrl: string | null;
  hasMedia: boolean;
}

interface MediaDimensions {
  width: number;
  height: number;
}

export function selectMediaRender(file: PublicMemeFileRead | null | undefined): SelectedMediaRender {
  const render = file?.render;
  const videoUrl = render?.web_video_url ?? null;
  const imageUrl = render?.display_url ?? render?.preview_url ?? render?.thumbnail_url ?? render?.original_url ?? file?.render_url ?? null;
  const audioUrl = file?.mime_type?.startsWith('audio/') ? (file.render_url ?? render?.original_url ?? null) : null;
  const downloadUrl = render?.download_url ?? file?.download_url ?? null;

  return {
    imageUrl,
    videoUrl,
    audioUrl,
    downloadUrl,
    hasMedia: Boolean(videoUrl || imageUrl || audioUrl)
  };
}

export function selectMediaAspectRatio(file: PublicMemeFileRead | null | undefined): string | null {
  const dimensions = selectMediaDimensions(file);
  return dimensions ? `${dimensions.width} / ${dimensions.height}` : null;
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
