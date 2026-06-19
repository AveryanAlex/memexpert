import { json, type RequestHandler } from '@sveltejs/kit';
import { apiBaseUrl, forwardBackendAccessCookie } from '$lib/server/backend';

export const POST: RequestHandler = async ({ cookies, fetch, request }) => {
  const payload = await readJsonPayload(request);
  const initData = typeof payload?.initData === 'string' ? payload.initData : '';

  if (!initData.trim()) {
    return json({ detail: 'Telegram initData is required.' }, { status: 400 });
  }

  const headers = new Headers({
    accept: 'application/json',
    'content-type': 'application/json'
  });
  const cookie = request.headers.get('cookie');
  if (cookie) {
    headers.set('cookie', cookie);
  }

  let upstream: Response;
  try {
    upstream = await fetch(new URL('/api/v1/auth/telegram-miniapp', apiBaseUrl()), {
      method: 'POST',
      headers,
      body: JSON.stringify({ initData })
    });
  } catch {
    return json({ detail: 'Could not reach the Telegram Mini App auth API.' }, { status: 502 });
  }

  forwardBackendAccessCookie(upstream, cookies);

  const upstreamPayload = await readResponsePayload(upstream);
  const publicPayload = stripAccessToken(upstreamPayload);
  if (publicPayload === null) {
    return new Response(null, { status: upstream.status, statusText: upstream.statusText });
  }

  return json(publicPayload, { status: upstream.status, statusText: upstream.statusText });
};

async function readJsonPayload(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const payload = (await request.json()) as unknown;
    return isRecord(payload) ? payload : null;
  } catch {
    return null;
  }
}

async function readResponsePayload(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { detail: text };
  }
}

function stripAccessToken(payload: unknown): unknown {
  if (Array.isArray(payload)) {
    return payload.map(stripAccessToken);
  }

  if (!isRecord(payload)) {
    return payload;
  }

  const sanitized: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    if (key !== 'access_token') {
      sanitized[key] = stripAccessToken(value);
    }
  }
  return sanitized;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}
