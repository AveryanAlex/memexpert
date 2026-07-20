import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

export type MemeProxyAction = 'detail-click' | 'download' | 'favorite' | 'impression' | 'pin' | 'report' | 'save' | 'share' | 'view';
export type MemeProxyMethod = 'DELETE' | 'POST';
export type { ProxyFetch };

interface MemeActionProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
  memeId: string;
  action: MemeProxyAction;
  method: MemeProxyMethod;
}

export async function proxyMemeAction({
  fetch,
  request,
  apiBaseUrl,
  memeId,
  action,
  method
}: MemeActionProxyRequest): Promise<Response> {
  const headers = new Headers({ accept: 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) {
    headers.set('cookie', cookie);
  }
  const requestedWith = request.headers.get('x-requested-with');
  if (requestedWith) {
    headers.set('x-requested-with', requestedWith);
  }

  const bodyText = await request.text();
  if (bodyText) {
    headers.set('content-type', request.headers.get('content-type') ?? 'application/json');
  }

  const upstream = await fetch(new URL(`/api/v1/memes/${encodeURIComponent(memeId)}/${action}`, apiBaseUrl), {
    method,
    headers,
    body: bodyText || undefined,
    signal: request.signal
  });

  return passthroughUpstreamResponse(upstream);
}
