import { json, type Cookies } from '@sveltejs/kit';

import { DEFAULT_PAGE_SIZE, ApiError, fetchMemePage, type ApiFetch } from '$lib/api/client';
import { parseSearchParams } from '$lib/searchParams';
import { forwardBackendAccessCookie } from './backend';

interface MemePageProxyRequest {
  fetch: ApiFetch;
  request: Request;
  cookies: Cookies;
  apiBaseUrl: string;
  mode: 'browse' | 'search';
}

export async function proxyMemePage({ fetch, request, cookies, apiBaseUrl, mode }: MemePageProxyRequest): Promise<Response> {
  const url = new URL(request.url);
  const params = new URLSearchParams(url.searchParams);
  const query = mode === 'search' ? (params.get('query') ?? params.get('q') ?? '').trim() : '';
  if (query) {
    params.set('q', query);
  }

  const filters = parseSearchParams(params);

  try {
    const page = await fetchMemePage({
      fetch,
      baseUrl: apiBaseUrl,
      query,
      tags: filters.tags,
      includeNsfw: params.has('include_nsfw') ? filters.includeNsfw : undefined,
      mediaType: filters.mediaType,
      language: filters.language,
      scope: filters.scope,
      collectionIds: filters.collectionIds,
      limit: readPositiveInt(params.get('limit'), DEFAULT_PAGE_SIZE),
      offset: filters.offset,
      cookieHeader: request.headers.get('cookie') ?? undefined,
      onResponse: (response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    });

    return json(page);
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 502;
    const detail = error instanceof Error ? error.message : 'Could not reach the meme catalog API.';
    return json({ detail }, { status });
  }
}

function readPositiveInt(raw: string | null, fallback: number): number {
  const value = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}
