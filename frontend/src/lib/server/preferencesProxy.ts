import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

export type { ProxyFetch };

interface PreferencesProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

export async function proxyUserPreferences({
  fetch,
  request,
  apiBaseUrl
}: PreferencesProxyRequest): Promise<Response> {
  const headers = new Headers({ accept: 'application/json', 'content-type': 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) {
    headers.set('cookie', cookie);
  }
  const requestedWith = request.headers.get('x-requested-with');
  if (requestedWith) {
    headers.set('x-requested-with', requestedWith);
  }

  const upstream = await fetch(new URL('/api/v1/auth/preferences', apiBaseUrl), {
    method: 'PATCH',
    headers,
    body: await request.text()
  });

  return passthroughUpstreamResponse(upstream);
}
