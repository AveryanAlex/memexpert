import type { PublicMemeFileRead } from '$lib/api/types';

export interface SelectedMediaRender {
  imageUrl: string | null;
  videoUrl: string | null;
  audioUrl: string | null;
  downloadUrl: string | null;
  hasMedia: boolean;
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
