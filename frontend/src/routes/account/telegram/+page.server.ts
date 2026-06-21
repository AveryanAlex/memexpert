import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchCurrentSession, startTelegramLink } from '$lib/api/client';
import { apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ parent, url }) => {
  const layoutData = await parent();
  return {
    session: layoutData.session,
    sessionError: layoutData.sessionError,
    returnTo: sanitizeReturnTo(url.searchParams.get('returnTo'))
  };
};

export const actions: Actions = {
  start: async ({ cookies, fetch, request }) => {
    let cookieHeader = request.headers.get('cookie') ?? undefined;
    let bootstrappedToken: string | null = null;

    try {
      const session = await fetchCurrentSession({
        fetch,
        baseUrl: apiBaseUrl(),
        cookieHeader,
        onResponse: (response) => {
          bootstrappedToken = forwardBackendAccessCookie(response, cookies);
        }
      });

      if (session.user.account_type === 'full') {
        return {
          status: 'already-full',
          message: session.linked_providers.telegram_linked
            ? 'Telegram is already connected to this profile.'
            : 'This full profile cannot use the guest Telegram deep-link flow.'
        };
      }

      cookieHeader = cookieHeaderWithAccessToken(cookieHeader, bootstrappedToken);
      const link = await startTelegramLink({
        fetch,
        baseUrl: apiBaseUrl(),
        cookieHeader
      });

      return { status: 'started', link };
    } catch (error) {
      return fail(400, {
        status: 'error',
        message: error instanceof ApiError ? error.message : 'Could not start Telegram linking.'
      });
    }
  }
};

function sanitizeReturnTo(rawReturnTo: string | null): string {
  if (!rawReturnTo?.startsWith('/')) {
    return '/';
  }

  if (rawReturnTo.startsWith('//')) {
    return '/';
  }

  return rawReturnTo;
}
