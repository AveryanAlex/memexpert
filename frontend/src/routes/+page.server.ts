import type { Cookies } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';
import {
  DEFAULT_PAGE_SIZE,
  ApiError,
  emptyMemePage,
  fetchCurrentSession,
  fetchHomeFeed,
  fetchMemeOfTheDay,
  fetchMemePage,
  type ApiFetch
} from '$lib/api/client';
import type { PublicMemeOfTheDayRead } from '$lib/api/types';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request, setHeaders, url }) => {
  setHeaders({ 'cache-control': 'private, no-store' });
  const query = (url.searchParams.get('q') ?? '').trim();
  const offset = readOffset(url.searchParams.get('offset'));
  const upstreamFetch: ApiFetch = (input, init) => fetch(input, { ...init, signal: request.signal });
  const parentData = await parent();
  const cookieHeader = cookieHeaderWithAccessToken(
    request.headers.get('cookie') ?? undefined,
    cookies.get(ACCESS_COOKIE_NAME) ?? null
  );
  const backendBaseUrl = apiBaseUrl();
  const feedSource = query ? 'catalog' : 'home';
  const parentViewerId = parentData.session?.user.id ?? null;
  let issuedHomeAccessToken: string | null = null;
  const memeOfTheDayPromise = loadMemeOfTheDay({ fetch: upstreamFetch, baseUrl: backendBaseUrl, cookieHeader, cookies });

  try {
    const [page, memeOfTheDayResult] = await Promise.all([
      feedSource === 'home'
        ? fetchHomeFeed({
            fetch: upstreamFetch,
            baseUrl: backendBaseUrl,
            limit: DEFAULT_PAGE_SIZE,
            offset,
            cookieHeader,
            onResponse: (response) => {
              issuedHomeAccessToken = forwardBackendAccessCookie(response, cookies) ?? issuedHomeAccessToken;
            }
          })
        : fetchMemePage({
            fetch: upstreamFetch,
            baseUrl: backendBaseUrl,
            query,
            limit: DEFAULT_PAGE_SIZE,
            offset,
            cookieHeader
          }),
      memeOfTheDayPromise
    ]);
    const pageViewerId = feedSource === 'home'
      ? await resolveHomePageViewerId({
          fetch: upstreamFetch,
          baseUrl: backendBaseUrl,
          cookieHeader,
          cookies,
          parentViewerId,
          issuedAccessToken: issuedHomeAccessToken
        })
      : parentViewerId;

    return {
      page,
      pageViewerId,
      query,
      offset,
      feedSource,
      errorMessage: null,
      memeOfTheDay: memeOfTheDayResult.memeOfTheDay,
      memeOfTheDayErrorMessage: memeOfTheDayResult.errorMessage
    };
  } catch (error) {
    const memeOfTheDayResult = await memeOfTheDayPromise;
    const pageViewerId = feedSource === 'home'
      ? await resolveHomePageViewerId({
          fetch: upstreamFetch,
          baseUrl: backendBaseUrl,
          cookieHeader,
          cookies,
          parentViewerId,
          issuedAccessToken: issuedHomeAccessToken
        })
      : parentViewerId;

    if (error instanceof ApiError) {
      return {
        page: emptyMemePage(DEFAULT_PAGE_SIZE, offset),
        pageViewerId,
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
      pageViewerId,
      query,
      offset,
      feedSource,
      errorMessage: 'Could not reach the meme catalog API.',
      memeOfTheDay: memeOfTheDayResult.memeOfTheDay,
      memeOfTheDayErrorMessage: memeOfTheDayResult.errorMessage
    };
  }
};

interface HomePageViewerRequest {
  fetch: ApiFetch;
  baseUrl: string;
  cookieHeader?: string;
  cookies: Cookies;
  parentViewerId: string | null;
  issuedAccessToken: string | null;
}

async function resolveHomePageViewerId({
  fetch,
  baseUrl,
  cookieHeader,
  cookies,
  parentViewerId,
  issuedAccessToken
}: HomePageViewerRequest): Promise<string | null> {
  if (parentViewerId || !issuedAccessToken) {
    return parentViewerId;
  }

  try {
    const session = await fetchCurrentSession({
      fetch,
      baseUrl,
      cookieHeader: cookieHeaderWithAccessToken(cookieHeader, issuedAccessToken),
      onResponse: (response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    });
    return session.user.id;
  } catch {
    return null;
  }
}

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
