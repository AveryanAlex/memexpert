import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

export type { ProxyFetch };

interface PageViewProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

/** Proxy a small same-origin telemetry write with cookies and CSRF protection. */
export async function proxyPageView({ fetch, request, apiBaseUrl }: PageViewProxyRequest): Promise<Response> {
  const headers = new Headers({
    accept: 'application/json',
    'content-type': 'application/json'
  });
  const cookie = request.headers.get('cookie');
  if (cookie) headers.set('cookie', cookie);
  const requestedWith = request.headers.get('x-requested-with');
  if (requestedWith) headers.set('x-requested-with', requestedWith);

  const upstream = await fetch(new URL('/api/v1/analytics/page-views', apiBaseUrl), {
    method: 'POST',
    headers,
    body: await request.text(),
    signal: request.signal
  });
  return passthroughUpstreamResponse(upstream);
}
