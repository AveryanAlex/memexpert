export type MemeProxyAction = 'favorite' | 'pin' | 'save';
export type MemeProxyMethod = 'DELETE' | 'POST';
export type ProxyFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

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

  const upstream = await fetch(new URL(`/api/v1/memes/${encodeURIComponent(memeId)}/${action}`, apiBaseUrl), {
    method,
    headers
  });

  const responseHeaders = new Headers();
  const contentType = upstream.headers.get('content-type');
  if (contentType) {
    responseHeaders.set('content-type', contentType);
  }

  for (const setCookie of readSetCookieHeaders(upstream.headers)) {
    responseHeaders.append('set-cookie', setCookie);
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders
  });
}

function readSetCookieHeaders(headers: Headers): string[] {
  const headersWithSetCookie = headers as Headers & { getSetCookie?: () => string[] };
  if (typeof headersWithSetCookie.getSetCookie === 'function') {
    return headersWithSetCookie.getSetCookie();
  }

  const setCookie = headers.get('set-cookie');
  return setCookie ? [setCookie] : [];
}
