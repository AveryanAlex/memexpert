import { json, type Cookies } from '@sveltejs/kit';

import { ApiError, DEFAULT_PAGE_SIZE, fetchSimilarMemes, type ApiFetch } from '$lib/api/client';
import { forwardBackendAccessCookie } from './backend';

const PRIVATE_JSON_HEADERS = { 'cache-control': 'private, no-store' } as const;

interface SimilarMemeProxyRequest {
  fetch: ApiFetch;
  request: Request;
  cookies: Cookies;
  apiBaseUrl: string;
  memeId: string;
}

export async function proxySimilarMemes({
  fetch,
  request,
  cookies,
  apiBaseUrl,
  memeId
}: SimilarMemeProxyRequest): Promise<Response> {
  const url = new URL(request.url);
  const limit = readInteger(url.searchParams.get('limit'), 'limit', DEFAULT_PAGE_SIZE, 1, 100);
  const offset = readInteger(url.searchParams.get('offset'), 'offset', 0, 0);

  if (typeof limit === 'string' || typeof offset === 'string') {
    return json(
      { detail: typeof limit === 'string' ? limit : offset },
      { status: 400, headers: PRIVATE_JSON_HEADERS }
    );
  }

  try {
    const page = await fetchSimilarMemes({
      fetch: (input, init) => fetch(input, { ...init, signal: request.signal }),
      baseUrl: apiBaseUrl,
      memeId,
      limit,
      offset,
      cookieHeader: request.headers.get('cookie') ?? undefined,
      onResponse: (response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    });

    return json(page, { headers: PRIVATE_JSON_HEADERS });
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 502;
    const detail = error instanceof Error ? error.message : 'Could not reach the meme catalog API.';
    return json({ detail }, { status, headers: PRIVATE_JSON_HEADERS });
  }
}

function readInteger(
  raw: string | null,
  name: string,
  fallback: number,
  minimum: number,
  maximum = Number.MAX_SAFE_INTEGER
): number | string {
  if (raw === null) return fallback;
  if (!/^\d+$/.test(raw)) return `${name} must be an integer between ${minimum} and ${maximum}.`;

  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    return `${name} must be an integer between ${minimum} and ${maximum}.`;
  }
  return value;
}
