import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { DEFAULT_PAGE_SIZE, ApiError, createCollection, emptyMemePage, fetchCollections, fetchHomeFeed, fetchMemePage } from '$lib/api/client';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request, url }) => {
  const query = (url.searchParams.get('q') ?? '').trim();
  const offset = readOffset(url.searchParams.get('offset'));
  const { session } = await parent();
  const cookieHeader = cookieHeaderWithAccessToken(
    request.headers.get('cookie') ?? undefined,
    cookies.get(ACCESS_COOKIE_NAME) ?? null
  );
  const feedSource = query ? 'catalog' : 'home';

  try {
    const [page, collections] = await Promise.all([
      feedSource === 'home'
        ? fetchHomeFeed({
            fetch,
            baseUrl: apiBaseUrl(),
            limit: DEFAULT_PAGE_SIZE,
            offset,
            cookieHeader
          })
        : fetchMemePage({
            fetch,
            baseUrl: apiBaseUrl(),
            query,
            limit: DEFAULT_PAGE_SIZE,
            offset,
            cookieHeader
          }),
      session
        ? fetchCollections({
            fetch,
            baseUrl: apiBaseUrl(),
            cookieHeader,
            onResponse: (response) => {
              forwardBackendAccessCookie(response, cookies);
            }
          }).catch(() => null)
        : Promise.resolve(null)
    ]);

    return { page, collections, query, offset, feedSource, errorMessage: null };
  } catch (error) {
    if (error instanceof ApiError) {
      return {
        page: emptyMemePage(DEFAULT_PAGE_SIZE, offset),
        collections: null,
        query,
        offset,
        feedSource,
        errorMessage: error.message
      };
    }

    return {
      page: emptyMemePage(DEFAULT_PAGE_SIZE, offset),
      collections: null,
      query,
      offset,
      feedSource,
      errorMessage: 'Could not reach the meme catalog API.'
    };
  }
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

function readOffset(raw: string | null): number {
  const offset = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(offset) && offset > 0 ? offset : 0;
}
