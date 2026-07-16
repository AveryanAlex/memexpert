import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminSourceBackfills, fetchAdminSourceChannelPosts, fetchAdminSourceChannels, fetchAdminTelegramSessions } from '$lib/api/client';
import {
  assignSourceChannel,
  backfillSourceChannel,
  markSourceChannelDead,
  orphanSourceChannel,
  replaySourcePost,
  resumeSourceBackfill,
  toggleSourceChannel,
  updateSourceChannelIngestion,
  validateSourceAccount
} from '$lib/server/admin/sourceActions';
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

  const [sourceChannelsResult, postPageResult, telegramAccountsResult, backfillsResult] = await Promise.allSettled([
    fetchAdminSourceChannels(api),
    fetchAdminSourceChannelPosts(api, params.channelId, {
      limit: SOURCE_POST_PAGE_SIZE,
      offset,
      snapshotAt,
      status
    }),
    fetchAdminTelegramSessions(api),
    fetchAdminSourceBackfills(api, params.channelId)
  ]);

  const sourceChannels = settledValue(sourceChannelsResult, []);
  const postPage = settledValue(postPageResult, null);
  const telegramAccounts = settledValue(telegramAccountsResult, []);
  const backfills = settledValue(backfillsResult, { items: [] });
  const source = sourceChannels.find((candidate) => candidate.id === params.channelId) ?? null;
  const recoveryRequestIds = {
    backfills: Object.fromEntries(backfills.items.map((job) => [job.id, crypto.randomUUID()])),
    posts: Object.fromEntries((postPage?.items ?? []).map((post) => [post.id, crypto.randomUUID()]))
  };

  return {
    source,
    postPage,
    backfills,
    backfillLoadError: loadErrorMessage(backfillsResult, 'Could not load backfill history.'),
    postLoadError: loadErrorMessage(postPageResult, 'Could not load fetched-message history.'),
    telegramAccountsLoadError: loadErrorMessage(telegramAccountsResult, 'Could not load Telegram accounts.'),
    recoveryRequestIds,
    telegramAccounts,
    paging: {
      page,
      snapshotAt: postPage?.snapshot_at ?? snapshotAt ?? new Date().toISOString(),
      status,
      hasPrevious: page > 1,
      hasNext: postPage ? postPage.offset + postPage.items.length < postPage.total : false
    },
    loadError: loadErrorMessage(sourceChannelsResult, 'Could not load source details.')
      ?? (source ? null : 'Source was not found.')
  };
};

export const actions: Actions = {
  toggleSourceChannel,
  updateSourceChannelIngestion,
  assignSourceChannel,
  orphanSourceChannel,
  validateSourceAccount,
  markSourceChannelDead,
  backfillSourceChannel,
  resumeSourceBackfill,
  replaySourcePost
};

function settledValue<T>(result: PromiseSettledResult<T>, fallback: T): T {
  return result.status === 'fulfilled' ? result.value : fallback;
}

function loadErrorMessage(result: PromiseSettledResult<unknown>, fallback: string): string | null {
  if (result.status === 'fulfilled') return null;
  return result.reason instanceof ApiError ? result.reason.message : fallback;
}
