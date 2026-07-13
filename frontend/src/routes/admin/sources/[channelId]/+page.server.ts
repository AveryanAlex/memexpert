import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminSourceChannelPosts, fetchAdminSourceChannels, fetchAdminTelegramSessions } from '$lib/api/client';
import { backfillSourceChannel } from '$lib/server/admin/sourceActions';
import { SOURCE_POST_PAGE_SIZE, sourcePostPageFromSearchParam } from '$lib/server/admin/sourcePostPagination';

export const load: PageServerLoad = async ({ fetch, params, request, url }) => {
  const page = sourcePostPageFromSearchParam(url.searchParams.get('page'));
  const snapshotAt = url.searchParams.get('snapshot_at');
  const offset = (page - 1) * SOURCE_POST_PAGE_SIZE;
  const api = {
    fetch,
    baseUrl: env.API_BASE_URL || 'http://localhost:8000',
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  try {
    const [sourceChannels, postPage, telegramAccounts] = await Promise.all([
      fetchAdminSourceChannels(api),
      fetchAdminSourceChannelPosts(api, params.channelId, {
        limit: SOURCE_POST_PAGE_SIZE,
        offset,
        snapshotAt
      }),
      fetchAdminTelegramSessions(api)
    ]);
    const source = sourceChannels.find((candidate) => candidate.id === params.channelId) ?? null;
    if (!source) {
      return {
        source: null,
        postPage: null,
        telegramAccounts,
        paging: { page, snapshotAt: snapshotAt ?? new Date().toISOString(), hasPrevious: page > 1, hasNext: false },
        loadError: 'Source was not found.'
      };
    }
    return {
      source,
      postPage,
      telegramAccounts,
      paging: {
        page,
        snapshotAt: postPage.snapshot_at,
        hasPrevious: page > 1,
        hasNext: postPage.offset + postPage.items.length < postPage.total
      },
      loadError: null
    };
  } catch (caught) {
    return {
      source: null,
      postPage: null,
      telegramAccounts: [],
      paging: { page, snapshotAt: snapshotAt ?? new Date().toISOString(), hasPrevious: page > 1, hasNext: false },
      loadError: caught instanceof ApiError ? caught.message : 'Could not load source indexing details.'
    };
  }
};

export const actions: Actions = { backfillSourceChannel };
