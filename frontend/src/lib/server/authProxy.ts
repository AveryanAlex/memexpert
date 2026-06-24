import { passthroughUpstreamResponse, type ProxyFetch } from './proxyResponse';

interface AuthProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
}

export function proxyAuthSession({ fetch, request, apiBaseUrl }: AuthProxyRequest): Promise<Response> {
  return proxyAuthEndpoint({ fetch, request, apiBaseUrl, path: '/api/v1/auth/session', method: 'GET' });
}

export function proxyTelegramLinkStart({ fetch, request, apiBaseUrl }: AuthProxyRequest): Promise<Response> {
  return proxyAuthEndpoint({ fetch, request, apiBaseUrl, path: '/api/v1/auth/link/telegram', method: 'POST' });
}

async function proxyAuthEndpoint({ fetch, request, apiBaseUrl, path, method }: AuthProxyRequest & { path: string; method: 'GET' | 'POST' }): Promise<Response> {
  const headers = new Headers({ accept: 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) headers.set('cookie', cookie);
  if (method !== 'GET') headers.set('x-requested-with', 'XMLHttpRequest');

  const upstream = await fetch(new URL(path, apiBaseUrl), { method, headers });
  return passthroughUpstreamResponse(upstream);
}
