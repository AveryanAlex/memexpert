import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminSourceBackfills, fetchAdminSourceChannelPosts, fetchAdminSourceChannels, fetchAdminTelegramSessions } from '$lib/api/client';
import { backfillSourceChannel, replaySourcePost, resumeSourceBackfill } from '$lib/server/admin/sourceActions';
import { SOURCE_POST_PAGE_SIZE, sourcePostPageFromSearchParam, sourcePostStatusFromSearchParam } from '$lib/server/admin/sourcePostPagination';

export const load: PageServerLoad = async ({ fetch, params, request, url }) => {
  const page = sourcePostPageFromSearchParam(url.searchParams.get('page'));
  const snapshotAt = url.searchParams.get('snapshot_at');
  const status = sourcePostStatusFromSearchParam(url.searchParams.get('status'));
  const offset = (page - 1) * SOURCE_POST_PAGE_SIZE;
  const api = {
    fetch,
    baseUrl: env.API_BASE_URL || 'http://localhost:8000',
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  try {
    const backfillsPromise = fetchAdminSourceBackfills(api, params.channelId)
      .then((backfills) => ({ backfills, backfillLoadError: null as string | null }))
      .catch((caught) => ({
        backfills: { items: [] },
        backfillLoadError: caught instanceof ApiError ? caught.message : 'Could not load backfill history.'
      }));
    const [sourceChannels, postPage, telegramAccounts] = await Promise.all([
      fetchAdminSourceChannels(api),
      fetchAdminSourceChannelPosts(api, params.channelId, {
        limit: SOURCE_POST_PAGE_SIZE,
        offset,
        snapshotAt,
        status
      }),
      fetchAdminTelegramSessions(api)
    ]);
    const { backfills, backfillLoadError } = await backfillsPromise;
    const recoveryRequestIds = {
      backfills: Object.fromEntries(backfills.items.map((job) => [job.id, crypto.randomUUID()])),
      posts: Object.fromEntries(postPage.items.map((post) => [post.id, crypto.randomUUID()]))
    };
    const source = sourceChannels.find((candidate) => candidate.id === params.channelId) ?? null;
    if (!source) {
      return {
        source: null,
        postPage: null,
        backfills,
        backfillLoadError,
        recoveryRequestIds,
        telegramAccounts,
        paging: { page, snapshotAt: snapshotAt ?? new Date().toISOString(), status, hasPrevious: page > 1, hasNext: false },
        loadError: 'Source was not found.'
      };
    }
    return {
      source,
      postPage,
      backfills,
      backfillLoadError,
      recoveryRequestIds,
      telegramAccounts,
      paging: {
        page,
        snapshotAt: postPage.snapshot_at,
        status,
        hasPrevious: page > 1,
        hasNext: postPage.offset + postPage.items.length < postPage.total
      },
      loadError: null
    };
  } catch (caught) {
    return {
      source: null,
      postPage: null,
      backfills: { items: [] },
      backfillLoadError: null,
      recoveryRequestIds: { backfills: {}, posts: {} },
      telegramAccounts: [],
      paging: { page, snapshotAt: snapshotAt ?? new Date().toISOString(), status, hasPrevious: page > 1, hasNext: false },
      loadError: caught instanceof ApiError ? caught.message : 'Could not load source indexing details.'
    };
  }
};

export const actions: Actions = { backfillSourceChannel, resumeSourceBackfill, replaySourcePost };
