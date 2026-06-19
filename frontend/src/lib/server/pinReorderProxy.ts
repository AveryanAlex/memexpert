import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

export type { ProxyFetch };

interface PinReorderProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

export async function proxyPinReorder({ fetch, request, apiBaseUrl }: PinReorderProxyRequest): Promise<Response> {
  const headers = new Headers({ accept: 'application/json', 'content-type': 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) {
    headers.set('cookie', cookie);
  }

  const requestedWith = request.headers.get('x-requested-with');
  if (requestedWith) {
    headers.set('x-requested-with', requestedWith);
  }

  const upstream = await fetch(new URL('/api/v1/memes/pins/reorder', apiBaseUrl), {
    method: 'PUT',
    headers,
    body: await request.text()
  });

  return passthroughUpstreamResponse(upstream);
}
