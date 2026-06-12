export type ProxyFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export function passthroughUpstreamResponse(upstream: Response): Response {
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
