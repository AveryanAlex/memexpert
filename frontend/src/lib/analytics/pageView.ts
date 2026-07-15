/** Privacy-bounded first-party consumer page-view tracking. */

export const CONSUMER_PAGE_VIEW_SURFACES = [
  'web_account',
  'web_collection',
  'web_home',
  'web_library',
  'web_meme_detail',
  'web_profile',
  'web_search',
  'web_tag',
  'web_template',
  'web_trends'
] as const;

export type ConsumerPageViewSurface = (typeof CONSUMER_PAGE_VIEW_SURFACES)[number];
export type PageViewFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/**
 * Reduce a consumer pathname to an approved coarse category. Returning null
 * for admin, API, auth, and unknown paths guarantees that this tracker never
 * submits a raw URL, route parameter, query string, or internal route name.
 */
export function consumerPageViewSurface(pathname: string): ConsumerPageViewSurface | null {
  if (pathname === '/') return 'web_home';
  if (pathname === '/search') return 'web_search';
  if (pathname === '/library') return 'web_library';
  if (pathname === '/profile') return 'web_profile';
  if (pathname === '/account/telegram') return 'web_account';
  if (pathname.startsWith('/collection/')) return 'web_collection';
  if (pathname.startsWith('/memes/')) return 'web_meme_detail';
  if (pathname.startsWith('/tags/')) return 'web_tag';
  if (pathname.startsWith('/templates/')) return 'web_template';
  if (pathname === '/trends' || pathname.startsWith('/trends/')) return 'web_trends';
  return null;
}

/** Send one coarse page-view category through the same-origin Svelte proxy. */
export async function recordPageView(surface: ConsumerPageViewSurface, request: PageViewFetch = fetch): Promise<void> {
  try {
    await request('/api/v1/analytics/page-views', {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
      headers: {
        accept: 'application/json',
        'content-type': 'application/json',
        'x-requested-with': 'XMLHttpRequest'
      },
      body: JSON.stringify({ surface })
    });
  } catch {
    // Analytics must never disrupt a route transition or render.
  }
}
