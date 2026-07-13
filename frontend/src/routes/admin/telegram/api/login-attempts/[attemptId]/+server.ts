import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

export const DELETE: RequestHandler = async ({ fetch, params, request }) => {
  const attemptId = params.attemptId.trim();
  if (!attemptId) {
    return Response.json({ detail: 'attempt_id is required.' }, { status: 400 });
  }

  return fetch(`${apiBaseUrl()}/api/v1/admin/telegram/login-attempts/${encodeURIComponent(attemptId)}`, {
    method: 'DELETE',
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
