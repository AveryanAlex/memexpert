import type { MemeResultAttributionRead, PublicMemeCardRead, PublicMemeDetailRead, PublicMemeFileRead } from '$lib/api/types';
import { selectMediaRender } from '$lib/media/render';

export type MemeActionKind = 'copy' | 'download' | 'favorite' | 'pin' | 'report' | 'save' | 'telegram' | 'unfavorite' | 'unpin' | 'unsave';

type MemeLike = PublicMemeCardRead | PublicMemeDetailRead;

export type MemeActionAttribution = Partial<
  Pick<
    MemeResultAttributionRead,
    | 'algorithm_version'
    | 'collection_ids'
    | 'collection_scope'
    | 'filters'
    | 'impression_id'
    | 'query'
    | 'rank'
    | 'reason'
    | 'request_id'
    | 'score'
    | 'score_components'
    | 'source_algorithm'
    | 'source_meme_id'
    | 'surface'
  >
>;

export interface MemeActionAttributionBody {
  attribution: MemeActionAttribution;
}

const ATTRIBUTION_QUERY_KEYS = {
  algorithm_version: 'attribution_algorithm_version',
  collection_id: 'attribution_collection_id',
  collection_scope: 'attribution_collection_scope',
  filters: 'attribution_filters',
  impression_id: 'attribution_impression_id',
  query: 'attribution_query',
  rank: 'attribution_rank',
  reason: 'attribution_reason',
  request_id: 'attribution_request_id',
  score: 'attribution_score',
  score_components: 'attribution_score_components',
  source_algorithm: 'attribution_source_algorithm',
  source_meme_id: 'attribution_source_meme_id',
  surface: 'attribution_surface'
} as const;

export function memeHref(meme: Pick<MemeLike, 'id' | 'seo_page_slug'>, attribution?: MemeActionAttribution | null): string {
  const href = `/memes/${meme.seo_page_slug || meme.id}`;
  const params = memeAttributionSearchParams(attribution);
  const query = params.toString();
  return query ? `${href}?${query}` : href;
}

export function canonicalMemeUrl(meme: Pick<MemeLike, 'id' | 'seo_page_slug'>, origin: string): string {
  return new URL(memeHref(meme), origin).toString();
}

export function memeAttributionSearchParams(attribution?: MemeActionAttribution | null): URLSearchParams {
  const params = new URLSearchParams();
  if (!attribution) return params;

  setParam(params, ATTRIBUTION_QUERY_KEYS.request_id, attribution.request_id);
  setParam(params, ATTRIBUTION_QUERY_KEYS.impression_id, attribution.impression_id);
  setParam(params, ATTRIBUTION_QUERY_KEYS.surface, attribution.surface);
  setParam(params, ATTRIBUTION_QUERY_KEYS.source_algorithm, attribution.source_algorithm);
  setParam(params, ATTRIBUTION_QUERY_KEYS.query, attribution.query);
  setParam(params, ATTRIBUTION_QUERY_KEYS.collection_scope, attribution.collection_scope);
  setParam(params, ATTRIBUTION_QUERY_KEYS.source_meme_id, attribution.source_meme_id);
  setParam(params, ATTRIBUTION_QUERY_KEYS.algorithm_version, attribution.algorithm_version);
  setParam(params, ATTRIBUTION_QUERY_KEYS.reason, attribution.reason);
  setJsonParam(params, ATTRIBUTION_QUERY_KEYS.filters, attribution.filters);
  setJsonParam(params, ATTRIBUTION_QUERY_KEYS.score_components, attribution.score_components);
  setNumberParam(params, ATTRIBUTION_QUERY_KEYS.rank, attribution.rank);
  setNumberParam(params, ATTRIBUTION_QUERY_KEYS.score, attribution.score);
  for (const collectionId of attribution.collection_ids ?? []) {
    setParam(params, ATTRIBUTION_QUERY_KEYS.collection_id, collectionId);
  }
  return params;
}

export function parseMemeAttributionSearchParams(params: URLSearchParams): MemeActionAttribution | null {
  if (![...params.keys()].some((key) => key.startsWith('attribution_'))) return null;

  return {
    request_id: readParam(params, ATTRIBUTION_QUERY_KEYS.request_id),
    impression_id: readParam(params, ATTRIBUTION_QUERY_KEYS.impression_id),
    surface: readParam(params, ATTRIBUTION_QUERY_KEYS.surface),
    source_algorithm: readParam(params, ATTRIBUTION_QUERY_KEYS.source_algorithm),
    rank: readIntegerParam(params, ATTRIBUTION_QUERY_KEYS.rank),
    query: readParam(params, ATTRIBUTION_QUERY_KEYS.query),
    filters: readJsonParam<NonNullable<MemeActionAttribution['filters']>>(params, ATTRIBUTION_QUERY_KEYS.filters),
    collection_scope: readParam(params, ATTRIBUTION_QUERY_KEYS.collection_scope),
    collection_ids: params.getAll(ATTRIBUTION_QUERY_KEYS.collection_id).filter(Boolean),
    source_meme_id: readParam(params, ATTRIBUTION_QUERY_KEYS.source_meme_id),
    algorithm_version: readParam(params, ATTRIBUTION_QUERY_KEYS.algorithm_version),
    score: readNumberParam(params, ATTRIBUTION_QUERY_KEYS.score),
    score_components: readJsonParam<NonNullable<MemeActionAttribution['score_components']>>(params, ATTRIBUTION_QUERY_KEYS.score_components),
    reason: readParam(params, ATTRIBUTION_QUERY_KEYS.reason)
  };
}

export function memeActionAttributionBody(attribution?: MemeActionAttribution | null): MemeActionAttributionBody | undefined {
  return attribution ? { attribution } : undefined;
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
  return firstUrl(meme.download_url, selectMediaRender(meme.primary_file).downloadUrl, ...readFileDownloadUrls(meme));
}

export function actionFailureMessage(action: MemeActionKind, error: unknown): string {
  const status = readStatus(error);
  const detail = readMessage(error);

  if ((action === 'pin' || action === 'unpin') && (status === 401 || status === 403)) {
    return 'Pinning requires a connected MemeXpert profile. Connect Telegram, then try again.';
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

function readFileDownloadUrls(meme: MemeLike): Array<string | null | undefined> {
  return 'files' in meme ? meme.files.map((file) => selectMediaRender(file).downloadUrl) : [];
}

function firstUrl(...urls: Array<string | null | undefined>): string | null {
  return urls.find((url) => typeof url === 'string' && url.length > 0) ?? null;
}

function setParam(params: URLSearchParams, key: string, value: string | null | undefined): void {
  const trimmed = value?.trim();
  if (trimmed) {
    params.append(key, trimmed);
  }
}

function setNumberParam(params: URLSearchParams, key: string, value: number | null | undefined): void {
  if (typeof value === 'number' && Number.isFinite(value)) {
    params.set(key, String(value));
  }
}

function setJsonParam(params: URLSearchParams, key: string, value: object | null | undefined): void {
  if (value && Object.keys(value).length > 0) {
    params.set(key, JSON.stringify(value));
  }
}

function readParam(params: URLSearchParams, key: string): string | undefined {
  return params.get(key)?.trim() || undefined;
}

function readNumberParam(params: URLSearchParams, key: string): number | undefined {
  const raw = params.get(key);
  if (raw === null) return undefined;
  const value = Number(raw);
  return Number.isFinite(value) ? value : undefined;
}

function readJsonParam<T>(params: URLSearchParams, key: string): T | undefined {
  const raw = params.get(key);
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as T) : undefined;
  } catch {
    return undefined;
  }
}

function readIntegerParam(params: URLSearchParams, key: string): number | undefined {
  const value = readNumberParam(params, key);
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : undefined;
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
