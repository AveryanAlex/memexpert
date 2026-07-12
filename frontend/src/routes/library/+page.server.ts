import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, createCollection } from '$lib/api/client';
import { loadLibraryPage } from '$lib/server/libraryPage';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request }) => {
  await parent();

  return loadLibraryPage({
    fetch,
    baseUrl: apiBaseUrl(),
    cookieHeader: cookieHeaderWithAccessToken(
      request.headers.get('cookie') ?? undefined,
      cookies.get(ACCESS_COOKIE_NAME) ?? null
    ),
    onResponse: (response) => {
      forwardBackendAccessCookie(response, cookies);
    }
  });
};

export const actions: Actions = {
  createCollection: async ({ cookies, fetch, request }) => {
    const form = await request.formData();
    try {
      const created = await createCollection({
        fetch,
        baseUrl: apiBaseUrl(),
        cookieHeader: request.headers.get('cookie') ?? undefined,
        body: {
          title: String(form.get('title') ?? ''),
          description: String(form.get('description') ?? ''),
          visibility: form.get('visibility') === 'unlisted' ? 'unlisted' : 'private'
        },
        onResponse: (response) => {
          forwardBackendAccessCookie(response, cookies);
        }
      });

      return { collectionCreatedId: created.collection.id, successMessage: 'Collection created.' };
    } catch (error) {
      return fail(error instanceof ApiError && error.status === 403 ? 403 : 400, {
        collectionError: error instanceof Error ? error.message : 'Could not create collection.'
      });
    }
  }
};
