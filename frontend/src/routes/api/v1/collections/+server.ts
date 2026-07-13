import { apiBaseUrl } from '$lib/server/backend';
import { proxyCollectionList } from '$lib/server/collectionListProxy';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ fetch, request }) => {
  return proxyCollectionList({ fetch, request, apiBaseUrl: apiBaseUrl() });
};
