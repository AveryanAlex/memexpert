import { json, type Cookies } from '@sveltejs/kit';

import { DEFAULT_PAGE_SIZE, ApiError, fetchHomeFeed, fetchMemePage, type ApiFetch } from '$lib/api/client';
import { parseSearchParams } from '$lib/searchParams';
import { forwardBackendAccessCookie } from './backend';

const PRIVATE_JSON_HEADERS = { 'cache-control': 'private, no-store' } as const;

interface MemePageProxyRequest {
  fetch: ApiFetch;
  request: Request;
  cookies: Cookies;
  apiBaseUrl: string;
  mode: 'browse' | 'home-feed' | 'search';
}

export async function proxyMemePage({ fetch, request, cookies, apiBaseUrl, mode }: MemePageProxyRequest): Promise<Response> {
  const url = new URL(request.url);
  const params = new URLSearchParams(url.searchParams);
  const query = mode === 'search' ? (params.get('query') ?? params.get('q') ?? '').trim() : '';
  if (query) {
    params.set('q', query);
  }

  const filters = parseSearchParams(params);
  const cursor = mode === 'home-feed' ? params.get('cursor')?.trim() || null : null;

  if (cursor && params.has('offset')) {
    return json(
      { detail: 'cursor and offset are mutually exclusive.' },
      { status: 400, headers: PRIVATE_JSON_HEADERS }
    );
  }

  try {
    const commonRequest = {
      fetch: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, { ...init, signal: request.signal }),
      baseUrl: apiBaseUrl,
      tags: filters.tags,
      includeNsfw: params.has('include_nsfw') ? filters.includeNsfw : undefined,
      mediaType: filters.mediaType,
      language: filters.language,
      limit: readPositiveInt(params.get('limit'), DEFAULT_PAGE_SIZE),
      cookieHeader: request.headers.get('cookie') ?? undefined,
      onResponse: (response: Response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    };
    const page = mode === 'home-feed'
      ? await fetchHomeFeed({ ...commonRequest, offset: cursor ? undefined : filters.offset, cursor })
      : await fetchMemePage({
          ...commonRequest,
          offset: filters.offset,
          query,
          scope: filters.scope,
          collectionIds: filters.collectionIds
        });

    return json(page, { headers: PRIVATE_JSON_HEADERS });
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 502;
    const detail = error instanceof Error ? error.message : 'Could not reach the meme catalog API.';
    const code = error instanceof ApiError ? error.code : undefined;
    return json({ ...(code ? { code } : {}), detail }, { status, headers: PRIVATE_JSON_HEADERS });
  }
}

function readPositiveInt(raw: string | null, fallback: number): number {
  const value = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}
