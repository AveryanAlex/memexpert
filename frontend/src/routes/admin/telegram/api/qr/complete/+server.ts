import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ fetch, request }) => {
  const payload = await request.json().catch(() => null);
  const sessionId = typeof payload?.session_id === 'string' ? payload.session_id.trim() : '';
  const attemptId = typeof payload?.attempt_id === 'string' ? payload.attempt_id.trim() : '';
  if (!sessionId) {
    return Response.json({ detail: 'session_id is required.' }, { status: 400 });
  }
  if (!attemptId) {
    return Response.json({ detail: 'attempt_id is required.' }, { status: 400 });
  }

  const body: { attempt_id: string; note?: string | null } = { attempt_id: attemptId };
  if ('note' in (payload ?? {})) {
    body.note = typeof payload?.note === 'string' && payload.note.trim() ? payload.note.trim() : null;
  }

  return fetch(`${apiBaseUrl()}/api/v1/admin/telegram/sessions/${encodeURIComponent(sessionId)}/login/qr/complete`, {
    method: 'POST',
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
      'x-requested-with': 'XMLHttpRequest',
      cookie: request.headers.get('cookie') ?? ''
    },
    body: JSON.stringify(body)
  });
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
