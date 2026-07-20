import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

interface InteractionBatchProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

/** Forward the browser's bounded interaction batch without exposing the backend origin. */
export async function proxyInteractionBatch({
  fetch,
  request,
  apiBaseUrl
}: InteractionBatchProxyRequest): Promise<Response> {
  const headers = new Headers({
    accept: 'application/json',
    'content-type': 'application/json'
  });
  const cookie = request.headers.get('cookie');
  if (cookie) headers.set('cookie', cookie);
  const requestedWith = request.headers.get('x-requested-with');
  if (requestedWith) headers.set('x-requested-with', requestedWith);

  const upstream = await fetch(new URL('/api/v1/analytics/interactions/batch', apiBaseUrl), {
    method: 'POST',
    headers,
    body: await request.text(),
    signal: request.signal
  });
  return passthroughUpstreamResponse(upstream);
}

export type { ProxyFetch };
