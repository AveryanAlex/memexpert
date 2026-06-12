import type { PublicMemeCardRead, PublicMemeDetailRead, PublicMemeFileRead } from '$lib/api/types';

export type MemeActionKind = 'copy' | 'download' | 'favorite' | 'pin' | 'report' | 'save' | 'telegram' | 'unfavorite' | 'unpin' | 'unsave';

type MemeLike = PublicMemeCardRead | PublicMemeDetailRead;

export function memeHref(meme: Pick<MemeLike, 'id' | 'seo_page_slug'>): string {
  return `/memes/${meme.seo_page_slug || meme.id}`;
}

export function canonicalMemeUrl(meme: Pick<MemeLike, 'id' | 'seo_page_slug'>, origin: string): string {
  return new URL(memeHref(meme), origin).toString();
}

export function telegramShareUrl(url: string, text?: string | null): string {
  const params = new URLSearchParams({ url });
  const trimmed = text?.trim();
  if (trimmed) {
    params.set('text', trimmed);
  }
  return `https://t.me/share/url?${params.toString()}`;
}

export function memeTitle(meme: Pick<MemeLike, 'caption' | 'tags'> & Partial<Pick<PublicMemeDetailRead, 'seo_title'>>): string {
  return meme.seo_title || meme.caption || meme.tags[0] || 'Untitled meme';
}

export function memeRenderUrl(meme: MemeLike): string | null {
  return firstUrl(meme.render_url, meme.primary_file?.render_url, ...readFileUrls(meme, 'render_url'));
}

export function memeDownloadUrl(meme: MemeLike): string | null {
  return firstUrl(meme.download_url, meme.primary_file?.download_url, ...readFileUrls(meme, 'download_url'));
}

export function actionFailureMessage(action: MemeActionKind, error: unknown): string {
  const status = readStatus(error);
  const detail = readMessage(error);

  if ((action === 'pin' || action === 'unpin') && (status === 401 || status === 403)) {
    return 'Pinning requires a full MemeXpert account. Link or sign in, then try again.';
  }

  if (action === 'report' && (status === 401 || status === 403)) {
    return detail ?? 'Reporting requires a full MemeXpert account. Link or sign in, then try again.';
  }

  if (action === 'report') {
    return detail ? `Could not submit report: ${detail}` : 'Could not submit report. Check your connection and try again.';
  }

  if (action === 'save' || action === 'unsave') {
    return detail ? `Could not update your active save collection: ${detail}` : 'Could not update your active save collection.';
  }

  if (action === 'download') {
    return 'Download is unavailable until this meme has a media download URL.';
  }

  return detail ? `Could not ${actionLabel(action)}: ${detail}` : `Could not ${actionLabel(action)}. Check your connection and try again.`;
}

function readFileUrls(meme: MemeLike, key: keyof Pick<PublicMemeFileRead, 'download_url' | 'render_url'>): Array<string | null | undefined> {
  return 'files' in meme ? meme.files.map((file) => file[key]) : [];
}

function firstUrl(...urls: Array<string | null | undefined>): string | null {
  return urls.find((url) => typeof url === 'string' && url.length > 0) ?? null;
}

function readStatus(error: unknown): number | null {
  return error && typeof error === 'object' && 'status' in error && typeof error.status === 'number' ? error.status : null;
}

function readMessage(error: unknown): string | null {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return null;
}

function actionLabel(action: MemeActionKind): string {
  switch (action) {
    case 'favorite':
      return 'like this meme';
    case 'unfavorite':
      return 'unlike this meme';
    case 'pin':
      return 'pin this meme';
    case 'unpin':
      return 'unpin this meme';
    case 'telegram':
      return 'share to Telegram';
    case 'copy':
      return 'copy the link';
    case 'report':
      return 'report this meme';
    default:
      return action;
  }
}
