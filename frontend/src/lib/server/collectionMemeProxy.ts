import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

interface CollectionMemeProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
  collectionId: string;
  memeId: string;
  method: 'DELETE' | 'POST';
}

export async function proxyCollectionMemeAction({
  fetch,
  request,
  apiBaseUrl,
  collectionId,
  memeId,
  method
}: CollectionMemeProxyRequest): Promise<Response> {
  const headers = new Headers({ accept: 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) {
    headers.set('cookie', cookie);
  }
  const requestedWith = request.headers.get('x-requested-with');
  if (requestedWith) {
    headers.set('x-requested-with', requestedWith);
  }
  const body = await request.text();
  const contentType = request.headers.get('content-type');
  if (body && contentType) {
    headers.set('content-type', contentType);
  }

  const upstream = await fetch(
    new URL(`/api/v1/collections/${encodeURIComponent(collectionId)}/memes/${encodeURIComponent(memeId)}`, apiBaseUrl),
    { method, headers, body: body || undefined, signal: request.signal }
  );

  return passthroughUpstreamResponse(upstream);
}

export type { ProxyFetch };
