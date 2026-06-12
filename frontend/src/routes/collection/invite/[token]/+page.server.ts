import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, joinCollectionInvite } from '$lib/api/client';
import { apiBaseUrl, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = ({ params }) => {
  return { token: params.token };
};

export const actions: Actions = {
  default: async ({ cookies, fetch, params, request }) => {
    let collectionId: string;
    try {
      const joined = await joinCollectionInvite({
        fetch,
        baseUrl: apiBaseUrl(),
        token: params.token,
        cookieHeader: request.headers.get('cookie') ?? undefined,
        onResponse: (response) => {
          forwardBackendAccessCookie(response, cookies);
        }
      });
      collectionId = joined.collection.id;
    } catch (error) {
      return fail(readStatus(error), {
        errorMessage: error instanceof Error ? error.message : 'Could not join this invite.'
      });
    }

    redirect(303, `/collection/${collectionId}`);
  }
};

function readStatus(error: unknown): 400 | 403 | 404 | 409 | 500 {
  if (error instanceof ApiError && [400, 403, 404, 409].includes(error.status)) {
    return error.status as 400 | 403 | 404 | 409;
  }
  return 500;
}
