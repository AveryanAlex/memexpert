import type { PublicMemeDetailRead, PublicMemeSearchPageRead } from './types';

export const DEFAULT_PAGE_SIZE = 12;

export type ApiFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface CatalogRequest {
  fetch: ApiFetch;
  baseUrl: string;
  cookieHeader?: string;
}

interface PageRequest extends CatalogRequest {
  query: string;
  limit: number;
  offset: number;
}

interface DetailRequest extends CatalogRequest {
  memeId: string;
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

export async function fetchMemeDetail(request: DetailRequest): Promise<PublicMemeDetailRead> {
  return apiGet<PublicMemeDetailRead>(
    `/api/v1/memes/${encodeURIComponent(request.memeId)}`,
    new URLSearchParams({ include_nsfw: 'false' }),
    request
  );
}

async function apiGet<T>(path: string, params: URLSearchParams, request: CatalogRequest): Promise<T> {
  const url = new URL(path, request.baseUrl);
  url.search = params.toString();

  const headers = new Headers({ accept: 'application/json' });
  if (request.cookieHeader) {
    headers.set('cookie', request.cookieHeader);
  }

  const response = await request.fetch(url, { headers });
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
