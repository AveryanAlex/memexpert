import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

interface MemeCollectionChoicesProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
  memeId: string;
}

export async function proxyMemeCollectionChoices({
  fetch,
  request,
  apiBaseUrl,
  memeId
}: MemeCollectionChoicesProxyRequest): Promise<Response> {
  const headers = new Headers({ accept: 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) headers.set('cookie', cookie);

  const upstream = await fetch(
    new URL(`/api/v1/collections/meme-choices/${encodeURIComponent(memeId)}`, apiBaseUrl),
    {
      method: 'GET',
      headers,
      signal: request.signal
    }
  );
  return passthroughUpstreamResponse(upstream);
}

export type { ProxyFetch };
