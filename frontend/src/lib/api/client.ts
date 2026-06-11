import type {
  CurrentSessionRead,
  PublicMemeDetailRead,
  PublicMemeLandingRead,
  PublicMemeSearchPageRead,
  TelegramLinkStartRead
} from './types';

export const DEFAULT_PAGE_SIZE = 12;

export type ApiFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface CatalogRequest {
  fetch: ApiFetch;
  baseUrl: string;
  cookieHeader?: string;
  onResponse?: (response: Response) => void;
}

interface PageRequest extends CatalogRequest {
  query: string;
  limit: number;
  offset: number;
}

interface DetailRequest extends CatalogRequest {
  memeId: string;
}

type FavoriteRequest = DetailRequest;

interface LandingRequest extends CatalogRequest {
  slug: string;
  limit: number;
  offset: number;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function fetchMemePage(request: PageRequest): Promise<PublicMemeSearchPageRead> {
  const query = request.query.trim();
  const params = new URLSearchParams({
    limit: String(request.limit),
    offset: String(request.offset)
  });

  if (query) {
    params.set('query', query);
    return apiGet<PublicMemeSearchPageRead>('/api/v1/memes/search', params, request);
  }

  return apiGet<PublicMemeSearchPageRead>('/api/v1/memes/browse', params, request);
}

export async function fetchCurrentSession(request: CatalogRequest): Promise<CurrentSessionRead> {
  return apiJson<CurrentSessionRead>('/api/v1/auth/session', undefined, request);
}

export async function refreshCurrentSession(request: CatalogRequest): Promise<CurrentSessionRead> {
  return apiJson<CurrentSessionRead>('/api/v1/auth/session/refresh', undefined, request, { method: 'POST' });
}

export async function startTelegramLink(request: CatalogRequest): Promise<TelegramLinkStartRead> {
  return apiJson<TelegramLinkStartRead>('/api/v1/auth/link/telegram', undefined, request, { method: 'POST' });
}

export async function fetchMemeDetail(request: DetailRequest): Promise<PublicMemeDetailRead> {
  const encoded = encodeURIComponent(request.memeId);
  const path = isUuid(request.memeId) ? `/api/v1/memes/${encoded}` : `/api/v1/memes/slug/${encoded}`;
  return apiGet<PublicMemeDetailRead>(
    path,
    new URLSearchParams({ include_nsfw: 'false' }),
    request
  );
}

export async function favoriteMeme(request: FavoriteRequest): Promise<void> {
  await apiJson<unknown>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/favorite`, undefined, request, {
    method: 'POST'
  });
}

export async function fetchTagLanding(request: LandingRequest): Promise<PublicMemeLandingRead> {
  return fetchLanding(`/api/v1/memes/tags/${encodeURIComponent(request.slug)}`, request);
}

export async function fetchTemplateLanding(request: LandingRequest): Promise<PublicMemeLandingRead> {
  return fetchLanding(`/api/v1/memes/templates/${encodeURIComponent(request.slug)}`, request);
}

async function fetchLanding(path: string, request: LandingRequest): Promise<PublicMemeLandingRead> {
  return apiGet<PublicMemeLandingRead>(
    path,
    new URLSearchParams({ limit: String(request.limit), offset: String(request.offset) }),
    request
  );
}

async function apiGet<T>(path: string, params: URLSearchParams, request: CatalogRequest): Promise<T> {
  return apiJson<T>(path, params, request);
}

async function apiJson<T>(
  path: string,
  params: URLSearchParams | undefined,
  request: CatalogRequest,
  init: RequestInit = {}
): Promise<T> {
  const url = new URL(path, request.baseUrl);
  if (params) {
    url.search = params.toString();
  }

  const headers = new Headers({ accept: 'application/json' });
  if (request.cookieHeader) {
    headers.set('cookie', request.cookieHeader);
  }

  const response = await request.fetch(url, { ...init, headers });
  request.onResponse?.(response);
  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(response.status, readErrorDetail(payload) ?? `Catalog API returned ${response.status}`);
  }

  return payload as T;
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function readErrorDetail(payload: unknown): string | null {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = payload.detail;
    return typeof detail === 'string' ? detail : null;
  }

  return null;
}

export function emptyMemePage(limit: number, offset: number): PublicMemeSearchPageRead {
  return {
    items: [],
    limit,
    offset,
    total: 0,
    has_more: false
  };
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}
