import { env } from '$env/dynamic/private';
import { proxyActiveSaveCollection } from '$lib/server/activeSaveProxy';
import type { RequestHandler } from './$types';

export const PUT: RequestHandler = ({ fetch, request }) => {
  return proxyActiveSaveCollection({ fetch, request, apiBaseUrl: apiBaseUrl() });
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
