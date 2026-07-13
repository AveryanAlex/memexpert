import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ fetch, request }) => {
  const payload = await request.json().catch(() => null);
  const attemptId = typeof payload?.attempt_id === 'string' ? payload.attempt_id.trim() : '';
  if (!attemptId) {
    return Response.json({ detail: 'attempt_id is required.' }, { status: 400 });
  }

  const body: { note?: string | null } = {};
  if ('note' in (payload ?? {})) {
    body.note = typeof payload?.note === 'string' && payload.note.trim() ? payload.note.trim() : null;
  }

  return fetch(`${apiBaseUrl()}/api/v1/admin/telegram/login-attempts/${encodeURIComponent(attemptId)}/qr/complete`, {
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
