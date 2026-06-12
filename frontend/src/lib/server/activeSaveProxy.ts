import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

export type { ProxyFetch };

interface ActiveSaveProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

export async function proxyActiveSaveCollection({
  fetch,
  request,
  apiBaseUrl
}: ActiveSaveProxyRequest): Promise<Response> {
  const headers = new Headers({ accept: 'application/json', 'content-type': 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) {
    headers.set('cookie', cookie);
  }

  const upstream = await fetch(new URL('/api/v1/memes/active-save-collection', apiBaseUrl), {
    method: 'PUT',
    headers,
    body: await request.text()
  });

  return passthroughUpstreamResponse(upstream);
}
