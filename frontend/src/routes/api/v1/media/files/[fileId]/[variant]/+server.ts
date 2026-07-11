import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';
import { proxyMediaFile } from '$lib/server/mediaFileProxy';

export const GET: RequestHandler = ({ fetch, params, request }) => {
  return proxyMediaFile({
    fetch,
    request,
    apiBaseUrl: env.API_BASE_URL || 'http://localhost:8000',
    fileId: params.fileId,
    variant: params.variant
  });
};
