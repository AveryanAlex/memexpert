import { env } from '$env/dynamic/private';
import { proxyUserPreferences } from '$lib/server/preferencesProxy';
import type { RequestHandler } from './$types';

export const PATCH: RequestHandler = ({ fetch, request }) => {
  return proxyUserPreferences({ fetch, request, apiBaseUrl: apiBaseUrl() });
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
