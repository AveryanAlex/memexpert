import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

interface CollectionListProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

export async function proxyCollectionList({ fetch, request, apiBaseUrl }: CollectionListProxyRequest): Promise<Response> {
  const headers = new Headers({ accept: 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) {
    headers.set('cookie', cookie);
  }

  const upstream = await fetch(new URL('/api/v1/collections', apiBaseUrl), {
    method: 'GET',
    headers,
    signal: request.signal
  });

  return passthroughUpstreamResponse(upstream);
}

export type { ProxyFetch };
