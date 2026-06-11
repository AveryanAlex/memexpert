import type { PublicMemeFileRead } from '$lib/api/types';

export interface SelectedMediaRender {
  imageUrl: string | null;
  videoUrl: string | null;
  downloadUrl: string | null;
  hasMedia: boolean;
}

export function selectMediaRender(file: PublicMemeFileRead | null | undefined): SelectedMediaRender {
  const render = file?.render;
  const videoUrl = render?.web_video_url ?? null;
  const imageUrl = render?.display_url ?? render?.preview_url ?? render?.thumbnail_url ?? render?.original_url ?? null;
  const downloadUrl = render?.download_url ?? null;

  return {
    imageUrl,
    videoUrl,
    downloadUrl,
    hasMedia: Boolean(videoUrl || imageUrl)
  };
}
