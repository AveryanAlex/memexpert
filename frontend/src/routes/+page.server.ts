import { fail, type Cookies } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { DEFAULT_PAGE_SIZE, ApiError, createCollection, emptyMemePage, fetchCollections, fetchHomeFeed, fetchMemeOfTheDay, fetchMemePage, type ApiFetch } from '$lib/api/client';
import type { PublicMemeOfTheDayRead } from '$lib/api/types';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request, url }) => {
  const query = (url.searchParams.get('q') ?? '').trim();
  const offset = readOffset(url.searchParams.get('offset'));
  const { session } = await parent();
  const cookieHeader = cookieHeaderWithAccessToken(
    request.headers.get('cookie') ?? undefined,
    cookies.get(ACCESS_COOKIE_NAME) ?? null
  );
  const backendBaseUrl = apiBaseUrl();
  const feedSource = query ? 'catalog' : 'home';
  const memeOfTheDayPromise = loadMemeOfTheDay({ fetch, baseUrl: backendBaseUrl, cookieHeader, cookies });

  try {
    const [page, collections, memeOfTheDayResult] = await Promise.all([
      feedSource === 'home'
        ? fetchHomeFeed({
            fetch,
            baseUrl: backendBaseUrl,
            limit: DEFAULT_PAGE_SIZE,
            offset,
            cookieHeader
          })
        : fetchMemePage({
            fetch,
            baseUrl: backendBaseUrl,
            query,
            limit: DEFAULT_PAGE_SIZE,
            offset,
            cookieHeader
          }),
      session
        ? fetchCollections({
            fetch,
            baseUrl: backendBaseUrl,
            cookieHeader,
            onResponse: (response) => {
              forwardBackendAccessCookie(response, cookies);
            }
          }).catch(() => null)
        : Promise.resolve(null),
      memeOfTheDayPromise
    ]);

    return {
      page,
      collections,
      query,
      offset,
      feedSource,
      errorMessage: null,
      memeOfTheDay: memeOfTheDayResult.memeOfTheDay,
      memeOfTheDayErrorMessage: memeOfTheDayResult.errorMessage
    };
  } catch (error) {
    const memeOfTheDayResult = await memeOfTheDayPromise;

    if (error instanceof ApiError) {
      return {
        page: emptyMemePage(DEFAULT_PAGE_SIZE, offset),
        collections: null,
        query,
        offset,
        feedSource,
        errorMessage: error.message,
        memeOfTheDay: memeOfTheDayResult.memeOfTheDay,
        memeOfTheDayErrorMessage: memeOfTheDayResult.errorMessage
      };
    }

    return {
      page: emptyMemePage(DEFAULT_PAGE_SIZE, offset),
      collections: null,
      query,
      offset,
      feedSource,
      errorMessage: 'Could not reach the meme catalog API.',
      memeOfTheDay: memeOfTheDayResult.memeOfTheDay,
      memeOfTheDayErrorMessage: memeOfTheDayResult.errorMessage
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

interface MemeOfTheDayLoadRequest {
  fetch: ApiFetch;
  baseUrl: string;
  cookieHeader?: string;
  cookies: Cookies;
}

interface MemeOfTheDayLoadResult {
  memeOfTheDay: PublicMemeOfTheDayRead | null;
  errorMessage: string | null;
}

async function loadMemeOfTheDay({ fetch, baseUrl, cookieHeader, cookies }: MemeOfTheDayLoadRequest): Promise<MemeOfTheDayLoadResult> {
  try {
    const memeOfTheDay = await fetchMemeOfTheDay({
      fetch,
      baseUrl,
      cookieHeader,
      onResponse: (response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    });

    return { memeOfTheDay, errorMessage: null };
  } catch (error) {
    return { memeOfTheDay: null, errorMessage: memeOfTheDayErrorMessage(error) };
  }
}

function memeOfTheDayErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  return 'Could not reach the Meme of the Day API.';
}
