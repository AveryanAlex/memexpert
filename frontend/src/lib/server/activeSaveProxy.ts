export type ProxyFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

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
