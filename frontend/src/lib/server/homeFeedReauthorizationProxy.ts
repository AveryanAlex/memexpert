import { parseSearchParams } from '$lib/searchParams';
import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

interface HomeFeedReauthorizationProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

/** Reauthorize saved Home cards under the same normalized filters that produced them. */
export async function proxyHomeFeedReauthorization({
  fetch,
  request,
  apiBaseUrl
}: HomeFeedReauthorizationProxyRequest): Promise<Response> {
  const incomingUrl = new URL(request.url);
  const filters = parseSearchParams(incomingUrl.searchParams);
  const upstreamUrl = new URL('/api/v1/memes/home-feed/reauthorize', apiBaseUrl);

  for (const tag of filters.tags) upstreamUrl.searchParams.append('tags', tag);
  if (incomingUrl.searchParams.has('include_nsfw')) {
    upstreamUrl.searchParams.set('include_nsfw', String(filters.includeNsfw));
  }
  if (filters.mediaType) upstreamUrl.searchParams.set('media_type', filters.mediaType);
  if (filters.language) upstreamUrl.searchParams.set('language', filters.language);

  const headers = new Headers({
    accept: 'application/json',
    'content-type': 'application/json'
  });
  const cookie = request.headers.get('cookie');
  if (cookie) headers.set('cookie', cookie);
  const requestedWith = request.headers.get('x-requested-with');
  if (requestedWith) headers.set('x-requested-with', requestedWith);

  const upstream = await fetch(upstreamUrl, {
    method: 'POST',
    headers,
    body: await request.text(),
    signal: request.signal
  });
  const response = passthroughUpstreamResponse(upstream);
  response.headers.set('cache-control', 'private, no-store');
  return response;
}

export type { ProxyFetch };
