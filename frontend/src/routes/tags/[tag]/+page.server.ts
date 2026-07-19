import type { PageServerLoad } from './$types';
import { apiBaseUrl } from '$lib/server/backend';
import { loadTaxonomyLanding } from '$lib/server/taxonomyLanding';

export const load: PageServerLoad = async ({ fetch, params, request, url }) => {
  return loadTaxonomyLanding({
    kind: 'tag',
    slug: params.tag,
    rawOffset: url.searchParams.get('offset'),
    fetch,
    baseUrl: apiBaseUrl(),
    cookieHeader: request.headers.get('cookie') ?? undefined
  });
};
