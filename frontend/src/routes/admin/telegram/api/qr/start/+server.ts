import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ fetch, request }) => {
  const payload = await request.json().catch(() => null);
  const sessionId = typeof payload?.telegram_session_id === 'string' ? payload.telegram_session_id.trim() : '';
  const body: { telegram_session_id?: string; note?: string | null } = {};
  if (sessionId) body.telegram_session_id = sessionId;
  if ('note' in (payload ?? {})) {
    body.note = typeof payload?.note === 'string' && payload.note.trim() ? payload.note.trim() : null;
  }

  return fetch(`${apiBaseUrl()}/api/v1/admin/telegram/login-attempts/qr`, {
    method: 'POST',
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
      'x-requested-with': 'XMLHttpRequest',
      cookie: request.headers.get('cookie') ?? ''
    },
    signal: request.signal,
    body: JSON.stringify(body)
  });
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
