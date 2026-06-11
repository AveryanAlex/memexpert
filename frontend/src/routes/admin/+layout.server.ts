import { env } from '$env/dynamic/private';
import { error, redirect } from '@sveltejs/kit';
import type { LayoutServerLoad } from './$types';
import { ApiError, fetchAdminSession } from '$lib/api/client';

export const load: LayoutServerLoad = async ({ fetch, request }) => {
  try {
    const session = await fetchAdminSession({
      fetch,
      baseUrl: apiBaseUrl(),
      cookieHeader: request.headers.get('cookie') ?? undefined
    });

    return { adminUser: session.user };
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 401) {
      throw redirect(303, '/?admin=signin-required');
    }
    if (caught instanceof ApiError && caught.status === 403) {
      throw error(403, 'Admin access is required.');
    }
    throw error(503, 'Could not verify the admin session.');
  }
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
