import type { ProxyFetch } from './proxyResponse';

export const MEDIA_FILE_VARIANTS = ['thumbnail', 'preview', 'original', 'download', 'web-video.mp4'] as const;
export type MediaFileVariant = (typeof MEDIA_FILE_VARIANTS)[number];

interface MediaFileProxyRequest {
  fetch: ProxyFetch;
  request: Request;
  apiBaseUrl: string;
  fileId: string;
  variant: string;
}

export async function proxyMediaFile({ fetch, request, apiBaseUrl, fileId, variant }: MediaFileProxyRequest): Promise<Response> {
  if (!isMediaFileVariant(variant)) {
    return jsonError(404, 'Media variant was not found.');
  }

  const upstreamUrl = new URL(
    `/api/v1/media/files/${encodeURIComponent(fileId)}/${encodeURIComponent(variant)}`,
    apiBaseUrl
  );
  const headers = new Headers({ accept: 'application/json' });
  const cookie = request.headers.get('cookie');
  if (cookie) headers.set('cookie', cookie);

  const upstream = await fetch(upstreamUrl, {
    method: 'GET',
    headers,
    redirect: 'manual'
  });

  if (upstream.status === 307) {
    const location = upstream.headers.get('location');
    if (!location) return jsonError(502, 'Media redirect was unavailable.');
    return new Response(null, { status: 307, headers: { location } });
  }

  const responseHeaders = new Headers();
  const contentType = upstream.headers.get('content-type');
  if (contentType) responseHeaders.set('content-type', contentType);
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders
  });
}

function isMediaFileVariant(value: string): value is MediaFileVariant {
  return MEDIA_FILE_VARIANTS.some((variant) => variant === value);
}

function jsonError(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { 'content-type': 'application/json' }
  });
}
