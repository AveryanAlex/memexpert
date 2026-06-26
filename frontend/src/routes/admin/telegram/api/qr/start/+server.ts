import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ fetch, request }) => {
  const payload = await request.json().catch(() => null);
  const sessionId = typeof payload?.session_id === 'string' ? payload.session_id.trim() : '';
  if (!sessionId) {
    return Response.json({ detail: 'session_id is required.' }, { status: 400 });
  }

  return fetch(`${apiBaseUrl()}/api/v1/admin/telegram/sessions/${encodeURIComponent(sessionId)}/login/qr/start`, {
    method: 'POST',
    headers: {
      accept: 'application/json',
      'x-requested-with': 'XMLHttpRequest',
      cookie: request.headers.get('cookie') ?? ''
    }
  });
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
