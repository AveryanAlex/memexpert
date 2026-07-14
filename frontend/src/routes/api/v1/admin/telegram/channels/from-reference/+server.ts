import { apiBaseUrl } from '$lib/server/backend';
import { proxyAdminSourceReference } from '$lib/server/adminSourceReferenceProxy';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ fetch, request }) => {
  return proxyAdminSourceReference({ fetch, request, apiBaseUrl: apiBaseUrl() });
};
