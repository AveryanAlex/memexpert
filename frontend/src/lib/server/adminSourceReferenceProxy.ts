import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

interface AdminSourceReferenceProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

export async function proxyAdminSourceReference({
  fetch,
  request,
  apiBaseUrl
}: AdminSourceReferenceProxyRequest): Promise<Response> {
  const headers = new Headers({ accept: 'application/json' });
  for (const name of ['content-type', 'cookie', 'x-requested-with']) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  const body = await request.text();
  const upstream = await fetch(new URL('/api/v1/admin/telegram/channels/from-reference', apiBaseUrl), {
    method: 'POST',
    headers,
    body: body || undefined
  });
  return passthroughUpstreamResponse(upstream);
}
