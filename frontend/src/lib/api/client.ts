import type {
  AdminBlockedPerceptualHashActionRead,
  AdminBlockedPerceptualHashRead,
  AdminMemeDestructiveActionRead,
  AdminMemeDetailRead,
  AdminMemeRead,
  AdminMemeSeoPageRead,
  AdminMemeSeoReviewRowRead,
  AdminMemeTemplateActionRead,
  AdminMemeTemplateRead,
  AdminAnalyticsAudienceRead,
  AdminAnalyticsContentRead,
  AdminAnalyticsEngagementRead,
  AdminAnalyticsOverviewRead,
  AdminAnalyticsSearchQueryDetailRead,
  AdminAnalyticsSearchQueryPageRead,
  AdminAnalyticsSearchQuerySort,
  AdminModerationDecisionRead,
  AdminModerationReportRead,
  AdminOverviewRead,
  AdminRecoveryBatchMutationPayload,
  AdminRecoveryBatchPreviewPayload,
  AdminRecoveryBatchRead,
  AdminRecoveryMutationPayload,
  AdminRecoverySummaryRead,
  AdminRecoveryWorkRead,
  AdminRecoveryWorkPageRead,
  AdminRecoveryBucket,
  AdminRecoveryWorkKind,
  AdminSearchSynonymCatalogRead,
  AdminSearchSynonymDraftUpdatePayload,
  AdminSearchSynonymLocale,
  AdminSearchSynonymMutationPayload,
  AdminSearchSynonymPublishPayload,
  AdminSearchSynonymResetPayload,
  AdminSearchSynonymSyncRetryPayload,
  AdminSearchSynonymSyncStateRead,
  AdminSourceBackfillPayload,
  AdminSourceBackfillListRead,
  AdminSourceRecoveryMutationPayload,
  AdminSessionRead,
  AdminSourceChannelRead,
  AdminSourcePostPageRead,
  AdminTelegramChannelAssignPayload,
  AdminTelegramChannelCreatePayload,
  AdminTelegramChannelFromReferencePayload,
  AdminTelegramChannelGroupRead,
  AdminTelegramChannelOrphanPayload,
  AdminTelegramChannelUpdatePayload,
  AdminTelegramLoginCompleteRead,
  AdminTelegramLoginCancelRead,
  AdminTelegramLoginPasswordPayload,
  AdminTelegramLoginPhoneCodePayload,
  AdminTelegramLoginPhoneStartPayload,
  AdminTelegramLoginPhoneStartRead,
  AdminTelegramLoginQrCompletePayload,
  AdminTelegramLoginQrStartPayload,
  AdminTelegramLoginQrStartRead,
  AdminTelegramLoginQrStatusRead,
  AdminTelegramSessionActionRead,
  AdminTelegramSessionCreatePayload,
  AdminTelegramSessionDeletePayload,
  AdminTelegramSessionRead,
  AdminTelegramSessionUpdatePayload,
  AdminTelegramSessionValidatePayload,
  AdminTelegramSessionValidateRead,
  ChannelSuggestionRead,
  CollectionInviteLinkRead,
  CollectionInviteRead,
  CollectionMemberRead,
  CollectionMembershipRole,
  CollectionVisibility,
  ContentKind,
  ContentLanguage,
  CurrentSessionRead,
  MemeCollectionChoicesRead,
  MemeFavoriteMutationRead,
  MemeLibraryRead,
  MemeSearchScope,
  PinnedMemeRead,
  ProfileStatsRead,
  PublicMemeDetailRead,
  PublicMemeOfTheDayRead,
  PublicMemeLandingRead,
  PublicMemePopularitySummaryRead,
  PublicMemeSearchPageRead,
  PublicMemeTrendPageRead,
  PublicTrendComparisonRead,
  PublicTrendSummaryRead,
  PublicTrendTimelinePageRead,
  SeoCatalogMemePageRead,
  SeoCatalogSummaryRead,
  SeoCatalogTagPageRead,
  SeoCatalogTemplatePageRead,
  MemeReportRead,
  ModerationReason,
  TelegramLinkStartRead,
  UserLanguage,
  UserRead,
  WebCollectionDetailRead,
  WebCollectionListRead,
  WebCollectionSummaryRead
} from './types';
import { memeAttributionSearchParams } from '$lib/memeActions';
import type { MemeActionAttribution } from '$lib/memeActions';

export const DEFAULT_PAGE_SIZE = 12;

export type ApiFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface CatalogRequest {
  fetch: ApiFetch;
  baseUrl: string;
  cookieHeader?: string;
  onResponse?: (response: Response) => void;
}

export interface AdminAnalyticsRangeRequest extends CatalogRequest {
  startDate?: string | null;
  endDate?: string | null;
}

interface MemePageFilterParams {
  tags?: string[];
  includeNsfw?: boolean;
  mediaType?: ContentKind | null;
  language?: ContentLanguage | null;
  limit: number;
  offset: number;
}

interface HomeFeedRequest extends CatalogRequest, MemePageFilterParams {}

interface PageRequest extends CatalogRequest, MemePageFilterParams {
  query: string;
  scope?: MemeSearchScope | null;
  collectionIds?: string[];
}

interface DetailRequest extends CatalogRequest {
  memeId: string;
  attribution?: MemeActionAttribution | null;
}

interface SimilarMemeRequest extends DetailRequest {
  limit: number;
  offset: number;
}

interface TrendRequest extends CatalogRequest {
  ranking?: 'trending' | 'fastest_rising' | 'most_liked';
  limit: number;
  offset: number;
}

interface TrendComparisonRequest extends CatalogRequest {
  items: string[];
}

interface TrendTimelineRequest extends CatalogRequest {
  granularity: 'month' | 'year';
  limit: number;
  offset: number;
}

interface MemeActionRequest {
  fetch: ApiFetch;
  baseUrl?: string;
  cookieHeader?: string;
  onResponse?: (response: Response) => void;
  memeId: string;
  body?: unknown;
  keepalive?: boolean;
}

interface MemeReportRequest extends MemeActionRequest {
  reason: ModerationReason;
  note?: string | null;
}

interface ActiveSaveCollectionRequest extends CatalogRequest {
  collectionId: string;
}

interface PreferenceMutationRequest {
  fetch: ApiFetch;
  baseUrl?: string;
  cookieHeader?: string;
  onResponse?: (response: Response) => void;
  body: UserPreferencesUpdate;
}

export interface RemoveActionResponse {
  removed?: boolean;
}

export interface MemeInteractionRecordedResponse {
  ok: boolean;
}

interface JsonMutationRequest extends CatalogRequest {
  body?: unknown;
}

export interface DeleteCollectionResponse {
  deleted?: boolean;
}

export interface SaveCollectionMemeResponse {
  saved?: boolean;
}

export interface CollectionFormPayload {
  title: string;
  description?: string | null;
  visibility?: CollectionVisibility;
}

export interface CollectionInvitePayload {
  role?: CollectionMembershipRole;
  label?: string | null;
  max_uses?: number | null;
  expires_in_hours?: number | null;
}

export interface CollectionMemberRolePayload {
  role: Exclude<CollectionMembershipRole, 'owner'>;
}

export interface PinReorderPayload {
  meme_ids: string[];
}

export interface UserPreferencesUpdate {
  nsfw_enabled?: boolean;
  language?: UserLanguage;
}

interface LandingRequest extends CatalogRequest {
  slug: string;
  limit: number;
  offset: number;
}

interface SeoCatalogPageRequest extends CatalogRequest {
  limit: number;
  offset: number;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function fetchMemePage(request: PageRequest): Promise<PublicMemeSearchPageRead> {
  const query = request.query.trim();
  const params = memePageParams(request);

  if (request.scope) {
    params.set('scope', request.scope);
  }

  if (request.scope === 'collections') {
    for (const collectionId of request.collectionIds ?? []) {
      const normalized = collectionId.trim();
      if (normalized) {
        params.append('collection_ids', normalized);
      }
    }
  }

  if (query) {
    params.set('query', query);
    return apiGet<PublicMemeSearchPageRead>('/api/v1/memes/search', params, request);
  }

  return apiGet<PublicMemeSearchPageRead>('/api/v1/memes/browse', params, request);
}

export async function fetchHomeFeed(request: HomeFeedRequest): Promise<PublicMemeSearchPageRead> {
  return apiGet<PublicMemeSearchPageRead>('/api/v1/memes/home-feed', memePageParams(request), request);
}

export async function fetchMemeOfTheDay(request: CatalogRequest): Promise<PublicMemeOfTheDayRead> {
  return apiGet<PublicMemeOfTheDayRead>('/api/v1/memes/meme-of-the-day', new URLSearchParams(), request);
}

export async function fetchCurrentSession(request: CatalogRequest): Promise<CurrentSessionRead> {
  return apiJson<CurrentSessionRead>('/api/v1/auth/session', undefined, request);
}

export async function fetchProfileStats(request: CatalogRequest): Promise<ProfileStatsRead> {
  return apiGet<ProfileStatsRead>('/api/v1/auth/profile-stats', new URLSearchParams(), request);
}

export async function startTelegramLink(request: CatalogRequest): Promise<TelegramLinkStartRead> {
  return apiJson<TelegramLinkStartRead>('/api/v1/auth/link/telegram', undefined, request, { method: 'POST' });
}

export async function fetchMemeDetail(request: DetailRequest): Promise<PublicMemeDetailRead> {
  const encoded = encodeURIComponent(request.memeId);
  const path = isUuid(request.memeId) ? `/api/v1/memes/${encoded}` : `/api/v1/memes/slug/${encoded}`;
  const params = new URLSearchParams({ include_nsfw: 'false' });
  const attributionParams = memeAttributionSearchParams(request.attribution);
  for (const [key, value] of attributionParams) {
    params.append(key, value);
  }
  return apiGet<PublicMemeDetailRead>(
    path,
    params,
    request
  );
}

export async function fetchSimilarMemes(request: SimilarMemeRequest): Promise<PublicMemeSearchPageRead> {
  return apiGet<PublicMemeSearchPageRead>(
    `/api/v1/memes/${encodeURIComponent(request.memeId)}/similar`,
    new URLSearchParams({ include_nsfw: 'false', limit: String(request.limit), offset: String(request.offset) }),
    request
  );
}

export async function fetchMemeLibrary(request: CatalogRequest): Promise<MemeLibraryRead> {
  return apiGet<MemeLibraryRead>('/api/v1/memes/library', new URLSearchParams(), request);
}

export async function updateActiveSaveCollection(request: ActiveSaveCollectionRequest): Promise<CurrentSessionRead['user']> {
  return apiWrite<CurrentSessionRead['user']>('/api/v1/memes/active-save-collection', 'PUT', {
    ...request,
    body: { collection_id: request.collectionId }
  });
}

export async function updateUserPreferences(request: PreferenceMutationRequest): Promise<UserRead> {
  return apiBrowserWrite<UserRead>('/api/v1/auth/preferences', 'PATCH', request);
}

export async function fetchTrendPage(request: TrendRequest): Promise<PublicMemeTrendPageRead> {
  const params = new URLSearchParams({
    ranking: request.ranking ?? 'trending',
    limit: String(request.limit),
    offset: String(request.offset)
  });
  return apiGet<PublicMemeTrendPageRead>('/api/v1/memes/trends', params, request);
}

export async function fetchTagTrendSummaries(request: TrendRequest): Promise<PublicTrendSummaryRead[]> {
  return apiGet<PublicTrendSummaryRead[]>(
    '/api/v1/memes/trends/tags',
    new URLSearchParams({ limit: String(request.limit), offset: String(request.offset) }),
    request
  );
}

export async function fetchTemplateTrendSummaries(request: TrendRequest): Promise<PublicTrendSummaryRead[]> {
  return apiGet<PublicTrendSummaryRead[]>(
    '/api/v1/memes/trends/templates',
    new URLSearchParams({ limit: String(request.limit), offset: String(request.offset) }),
    request
  );
}

export async function fetchTrendComparison(request: TrendComparisonRequest): Promise<PublicTrendComparisonRead> {
  const params = new URLSearchParams();
  for (const item of request.items) {
    const normalized = item.trim();
    if (normalized) {
      params.append('item', normalized);
    }
  }
  return apiGet<PublicTrendComparisonRead>('/api/v1/memes/trends/compare', params, request);
}

export async function fetchTrendTimeline(request: TrendTimelineRequest): Promise<PublicTrendTimelinePageRead> {
  return apiGet<PublicTrendTimelinePageRead>(
    '/api/v1/memes/trends/timeline',
    new URLSearchParams({ granularity: request.granularity, limit: String(request.limit), offset: String(request.offset) }),
    request
  );
}

export async function fetchMemePopularitySummary(request: DetailRequest): Promise<PublicMemePopularitySummaryRead> {
  return apiGet<PublicMemePopularitySummaryRead>(
    `/api/v1/memes/${encodeURIComponent(request.memeId)}/popularity`,
    new URLSearchParams({ include_nsfw: 'false' }),
    request
  );
}

export async function fetchCollections(request: CatalogRequest): Promise<WebCollectionListRead> {
  return apiGet<WebCollectionListRead>('/api/v1/collections', new URLSearchParams(), request);
}

export async function fetchMemeCollectionChoices(
  request: CatalogRequest & { memeId: string }
): Promise<MemeCollectionChoicesRead> {
  return apiGet<MemeCollectionChoicesRead>(
    `/api/v1/collections/meme-choices/${encodeURIComponent(request.memeId)}`,
    new URLSearchParams(),
    request
  );
}

export async function fetchCollectionDetail(request: CatalogRequest & { collectionId: string }): Promise<WebCollectionDetailRead> {
  return apiGet<WebCollectionDetailRead>(`/api/v1/collections/${encodeURIComponent(request.collectionId)}`, new URLSearchParams(), request);
}

export async function createCollection(request: CatalogRequest & { body: CollectionFormPayload }): Promise<WebCollectionSummaryRead> {
  return apiWrite<WebCollectionSummaryRead>('/api/v1/collections', 'POST', request);
}

export async function updateCollection(request: CatalogRequest & { collectionId: string; body: CollectionFormPayload }): Promise<WebCollectionSummaryRead> {
  return apiWrite<WebCollectionSummaryRead>(`/api/v1/collections/${encodeURIComponent(request.collectionId)}`, 'PATCH', request);
}

export async function deleteCollection(request: CatalogRequest & { collectionId: string }): Promise<DeleteCollectionResponse> {
  return apiWrite<DeleteCollectionResponse>(`/api/v1/collections/${encodeURIComponent(request.collectionId)}`, 'DELETE', request);
}

export async function setActiveSaveCollection(request: CatalogRequest & { collectionId: string }): Promise<{ active_save_collection_id: string | null }> {
  return apiWrite<{ active_save_collection_id: string | null }>(
    `/api/v1/collections/${encodeURIComponent(request.collectionId)}/active-save`,
    'PUT',
    request
  );
}

export async function saveMemeToCollection(
  request: CatalogRequest & { collectionId: string; memeId: string; body?: unknown }
): Promise<SaveCollectionMemeResponse> {
  return apiWrite<SaveCollectionMemeResponse>(
    `/api/v1/collections/${encodeURIComponent(request.collectionId)}/memes/${encodeURIComponent(request.memeId)}`,
    'POST',
    request
  );
}

export async function removeMemeFromCollection(request: CatalogRequest & { collectionId: string; memeId: string }): Promise<RemoveActionResponse> {
  return apiWrite<RemoveActionResponse>(
    `/api/v1/collections/${encodeURIComponent(request.collectionId)}/memes/${encodeURIComponent(request.memeId)}`,
    'DELETE',
    request
  );
}

export async function createCollectionInvite(
  request: CatalogRequest & { collectionId: string; body: CollectionInvitePayload }
): Promise<CollectionInviteLinkRead> {
  return apiWrite<CollectionInviteLinkRead>(`/api/v1/collections/${encodeURIComponent(request.collectionId)}/invites`, 'POST', request);
}

export async function revokeCollectionInvite(request: CatalogRequest & { collectionId: string; inviteId: string }): Promise<CollectionInviteRead> {
  return apiWrite<CollectionInviteRead>(
    `/api/v1/collections/${encodeURIComponent(request.collectionId)}/invites/${encodeURIComponent(request.inviteId)}`,
    'DELETE',
    request
  );
}

export async function updateCollectionMemberRole(
  request: CatalogRequest & { collectionId: string; memberUserId: string; body: CollectionMemberRolePayload }
): Promise<CollectionMemberRead> {
  return apiWrite<CollectionMemberRead>(
    `/api/v1/collections/${encodeURIComponent(request.collectionId)}/members/${encodeURIComponent(request.memberUserId)}`,
    'PATCH',
    request
  );
}

export async function removeCollectionMember(request: CatalogRequest & { collectionId: string; memberUserId: string }): Promise<RemoveActionResponse> {
  return apiWrite<RemoveActionResponse>(
    `/api/v1/collections/${encodeURIComponent(request.collectionId)}/members/${encodeURIComponent(request.memberUserId)}`,
    'DELETE',
    request
  );
}

export async function joinCollectionInvite(request: CatalogRequest & { token: string }): Promise<WebCollectionSummaryRead> {
  return apiWrite<WebCollectionSummaryRead>(`/api/v1/collections/invites/${encodeURIComponent(request.token)}/join`, 'POST', request);
}
export async function favoriteMeme(request: MemeActionRequest): Promise<MemeFavoriteMutationRead> {
  return apiMutation<MemeFavoriteMutationRead>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/favorite`, 'POST', request);
}

export async function unfavoriteMeme(request: MemeActionRequest): Promise<MemeFavoriteMutationRead> {
  return apiMutation<MemeFavoriteMutationRead>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/favorite`, 'DELETE', request);
}

export async function saveMeme(request: MemeActionRequest): Promise<unknown> {
  return apiMutation(`/api/v1/memes/${encodeURIComponent(request.memeId)}/save`, 'POST', request);
}

export async function removeSavedMeme(request: MemeActionRequest): Promise<RemoveActionResponse> {
  return apiMutation(`/api/v1/memes/${encodeURIComponent(request.memeId)}/save`, 'DELETE', request);
}

export async function pinMeme(request: MemeActionRequest): Promise<unknown> {
  return apiMutation(`/api/v1/memes/${encodeURIComponent(request.memeId)}/pin`, 'POST', request);
}

export async function unpinMeme(request: MemeActionRequest): Promise<RemoveActionResponse> {
  return apiMutation(`/api/v1/memes/${encodeURIComponent(request.memeId)}/pin`, 'DELETE', request);
}

export async function reorderPins(request: CatalogRequest & { body: PinReorderPayload }): Promise<PinnedMemeRead[]> {
  return apiWrite<PinnedMemeRead[]>('/api/v1/memes/pins/reorder', 'PUT', request);
}

export async function reportMeme(request: MemeReportRequest): Promise<MemeReportRead> {
  const attribution = readActionAttribution(request.body);
  return apiJsonWrite<MemeReportRead>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/report`, 'POST', request, {
    reason: request.reason,
    note: request.note ?? null,
    ...(attribution === undefined ? {} : { attribution })
  });
}

export async function recordMemeShare(request: MemeActionRequest): Promise<MemeInteractionRecordedResponse> {
  return apiMutation<MemeInteractionRecordedResponse>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/share`, 'POST', request);
}

export async function recordMemeImpression(request: MemeActionRequest): Promise<MemeInteractionRecordedResponse> {
  return apiMutation<MemeInteractionRecordedResponse>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/impression`, 'POST', request);
}

export async function recordMemeDetailClick(request: MemeActionRequest): Promise<MemeInteractionRecordedResponse> {
  return apiMutation<MemeInteractionRecordedResponse>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/detail-click`, 'POST', request);
}

export async function recordMemeDownload(request: MemeActionRequest): Promise<MemeInteractionRecordedResponse> {
  return apiMutation<MemeInteractionRecordedResponse>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/download`, 'POST', request);
}

export async function fetchTagLanding(request: LandingRequest): Promise<PublicMemeLandingRead> {
  return fetchLanding(`/api/v1/memes/tags/${encodeURIComponent(request.slug)}`, request);
}

export async function fetchTemplateLanding(request: LandingRequest): Promise<PublicMemeLandingRead> {
  return fetchLanding(`/api/v1/memes/templates/${encodeURIComponent(request.slug)}`, request);
}

export async function fetchSeoSummary(request: CatalogRequest): Promise<SeoCatalogSummaryRead> {
  return apiGet<SeoCatalogSummaryRead>('/api/v1/seo/summary', new URLSearchParams(), request);
}

export async function fetchSeoMemes(request: SeoCatalogPageRequest): Promise<SeoCatalogMemePageRead> {
  return apiGet<SeoCatalogMemePageRead>('/api/v1/seo/memes', seoPageParams(request), request);
}

export async function fetchSeoTags(request: SeoCatalogPageRequest): Promise<SeoCatalogTagPageRead> {
  return apiGet<SeoCatalogTagPageRead>('/api/v1/seo/tags', seoPageParams(request), request);
}

export async function fetchSeoTemplates(request: SeoCatalogPageRequest): Promise<SeoCatalogTemplatePageRead> {
  return apiGet<SeoCatalogTemplatePageRead>('/api/v1/seo/templates', seoPageParams(request), request);
}

export async function fetchPinterestFeed(request: SeoCatalogPageRequest): Promise<SeoCatalogMemePageRead> {
  return apiGet<SeoCatalogMemePageRead>('/api/v1/seo/pinterest-feed', seoPageParams(request), request);
}

export async function fetchAdminSession(request: CatalogRequest): Promise<AdminSessionRead> {
  return apiGet<AdminSessionRead>('/api/v1/admin/session', new URLSearchParams(), request);
}

export async function fetchAdminOverview(request: CatalogRequest): Promise<AdminOverviewRead> {
  return apiGet<AdminOverviewRead>('/api/v1/admin/overview', new URLSearchParams(), request);
}

export async function fetchAdminAnalyticsOverview(
  request: AdminAnalyticsRangeRequest
): Promise<AdminAnalyticsOverviewRead> {
  return apiGet<AdminAnalyticsOverviewRead>(
    '/api/v1/admin/analytics/overview',
    adminAnalyticsRangeParams(request),
    request
  );
}

export async function fetchAdminAnalyticsEngagement(
  request: AdminAnalyticsRangeRequest
): Promise<AdminAnalyticsEngagementRead> {
  return apiGet<AdminAnalyticsEngagementRead>(
    '/api/v1/admin/analytics/engagement',
    adminAnalyticsRangeParams(request),
    request
  );
}

export async function fetchAdminAnalyticsAudience(
  request: AdminAnalyticsRangeRequest
): Promise<AdminAnalyticsAudienceRead> {
  return apiGet<AdminAnalyticsAudienceRead>(
    '/api/v1/admin/analytics/audience',
    adminAnalyticsRangeParams(request),
    request
  );
}

export async function fetchAdminAnalyticsContent(
  request: AdminAnalyticsRangeRequest
): Promise<AdminAnalyticsContentRead> {
  return apiGet<AdminAnalyticsContentRead>(
    '/api/v1/admin/analytics/content',
    adminAnalyticsRangeParams(request),
    request
  );
}

export async function fetchAdminAnalyticsSearchQueries(
  request: AdminAnalyticsRangeRequest & { limit: number; offset: number; sort?: AdminAnalyticsSearchQuerySort }
): Promise<AdminAnalyticsSearchQueryPageRead> {
  const params = adminAnalyticsRangeParams(request);
  params.set('limit', String(request.limit));
  params.set('offset', String(request.offset));
  if (request.sort) params.set('sort', request.sort);
  return apiGet<AdminAnalyticsSearchQueryPageRead>('/api/v1/admin/analytics/search-queries', params, request);
}

export async function fetchAdminAnalyticsSearchQueryDetail(
  request: AdminAnalyticsRangeRequest & { queryKey: string }
): Promise<AdminAnalyticsSearchQueryDetailRead> {
  const params = adminAnalyticsRangeParams(request);
  params.set('query_key', request.queryKey);
  return apiGet<AdminAnalyticsSearchQueryDetailRead>(
    '/api/v1/admin/analytics/search-queries/detail',
    params,
    request
  );
}

export async function fetchAdminChannelSuggestions(request: CatalogRequest): Promise<ChannelSuggestionRead[]> {
  return apiGet<ChannelSuggestionRead[]>('/api/v1/admin/channel-suggestions', new URLSearchParams(), request);
}

export async function fetchAdminSourceChannels(request: CatalogRequest): Promise<AdminSourceChannelRead[]> {
  return apiGet<AdminSourceChannelRead[]>('/api/v1/admin/source-channels', new URLSearchParams(), request);
}

export async function fetchAdminSourceChannelPosts(
  request: CatalogRequest,
  channelId: string,
  pagination: { limit: number; offset: number; snapshotAt?: string | null; status?: string | null }
): Promise<AdminSourcePostPageRead> {
  const params = new URLSearchParams({ limit: String(pagination.limit), offset: String(pagination.offset) });
  if (pagination.snapshotAt) params.set('snapshot_at', pagination.snapshotAt);
  if (pagination.status) params.set('status', pagination.status);
  return apiGet<AdminSourcePostPageRead>(
    `/api/v1/admin/source-channels/${encodeURIComponent(channelId)}/posts`,
    params,
    request
  );
}

export async function fetchAdminSourceBackfills(
  request: CatalogRequest,
  channelId: string
): Promise<AdminSourceBackfillListRead> {
  return apiGet<AdminSourceBackfillListRead>(
    `/api/v1/admin/source-channels/${encodeURIComponent(channelId)}/backfills`,
    new URLSearchParams(),
    request
  );
}

export async function resumeAdminSourceBackfill(
  request: CatalogRequest & { body: AdminSourceRecoveryMutationPayload },
  channelId: string,
  jobId: string
): Promise<AdminRecoveryBatchRead> {
  return apiWrite<AdminRecoveryBatchRead>(
    `/api/v1/admin/source-channels/${encodeURIComponent(channelId)}/backfills/${encodeURIComponent(jobId)}/resume`,
    'POST',
    request
  );
}

export async function replayAdminSourcePost(
  request: CatalogRequest & { body: AdminSourceRecoveryMutationPayload },
  channelId: string,
  postId: string
): Promise<AdminRecoveryBatchRead> {
  return apiWrite<AdminRecoveryBatchRead>(
    `/api/v1/admin/source-channels/${encodeURIComponent(channelId)}/posts/${encodeURIComponent(postId)}/replay`,
    'POST',
    request
  );
}

export async function fetchAdminRecoverySummary(request: CatalogRequest): Promise<AdminRecoverySummaryRead> {
  return apiGet<AdminRecoverySummaryRead>('/api/v1/admin/recovery/summary', new URLSearchParams(), request);
}

export async function fetchAdminSearchSynonymCatalog(
  request: CatalogRequest,
  locale: AdminSearchSynonymLocale
): Promise<AdminSearchSynonymCatalogRead> {
  return apiGet<AdminSearchSynonymCatalogRead>(
    `/api/v1/admin/search-synonyms/${encodeURIComponent(locale)}`,
    new URLSearchParams(),
    request
  );
}

export async function updateAdminSearchSynonymDraft(
  request: CatalogRequest & { body: AdminSearchSynonymDraftUpdatePayload },
  locale: AdminSearchSynonymLocale
): Promise<AdminSearchSynonymCatalogRead> {
  return apiWrite<AdminSearchSynonymCatalogRead>(
    `/api/v1/admin/search-synonyms/${encodeURIComponent(locale)}/draft`,
    'PUT',
    request
  );
}

export async function importAdminSearchSynonymSeed(
  request: CatalogRequest & { body: AdminSearchSynonymMutationPayload },
  locale: AdminSearchSynonymLocale
): Promise<AdminSearchSynonymCatalogRead> {
  return apiWrite<AdminSearchSynonymCatalogRead>(
    `/api/v1/admin/search-synonyms/${encodeURIComponent(locale)}/draft/import-seed`,
    'POST',
    request
  );
}

export async function publishAdminSearchSynonymDraft(
  request: CatalogRequest & { body: AdminSearchSynonymPublishPayload },
  locale: AdminSearchSynonymLocale
): Promise<AdminSearchSynonymCatalogRead> {
  return apiWrite<AdminSearchSynonymCatalogRead>(
    `/api/v1/admin/search-synonyms/${encodeURIComponent(locale)}/draft/publish`,
    'POST',
    request
  );
}

export async function resetAdminSearchSynonymDraft(
  request: CatalogRequest & { body: AdminSearchSynonymResetPayload },
  locale: AdminSearchSynonymLocale
): Promise<AdminSearchSynonymCatalogRead> {
  return apiWrite<AdminSearchSynonymCatalogRead>(
    `/api/v1/admin/search-synonyms/${encodeURIComponent(locale)}/draft/reset`,
    'POST',
    request
  );
}

export async function fetchAdminSearchSynonymSyncState(
  request: CatalogRequest
): Promise<AdminSearchSynonymSyncStateRead> {
  return apiGet<AdminSearchSynonymSyncStateRead>(
    '/api/v1/admin/search-synonyms/sync',
    new URLSearchParams(),
    request
  );
}

export async function retryAdminSearchSynonymSync(
  request: CatalogRequest & { body: AdminSearchSynonymSyncRetryPayload }
): Promise<AdminSearchSynonymSyncStateRead> {
  return apiWrite<AdminSearchSynonymSyncStateRead>(
    '/api/v1/admin/search-synonyms/sync/retry',
    'POST',
    request
  );
}

export async function fetchAdminRecoveryWork(
  request: CatalogRequest,
  filters: {
    bucket?: AdminRecoveryBucket | null;
    kind?: AdminRecoveryWorkKind | null;
    source?: string | null;
    stage?: string | null;
    reason?: string | null;
    query?: string | null;
    cursor?: string | null;
    limit: number;
  }
): Promise<AdminRecoveryWorkPageRead> {
  const params = new URLSearchParams({ limit: String(filters.limit) });
  if (filters.bucket) params.set('bucket', filters.bucket);
  if (filters.kind) params.set('kind', filters.kind);
  const sourceIsUuid = filters.source && isUuid(filters.source);
  if (sourceIsUuid) params.set('source_channel_id', filters.source as string);
  if (filters.stage) params.set('stage', filters.stage);
  if (filters.reason) params.set('reason', filters.reason);
  if (filters.query) params.set('q', filters.query);
  else if (filters.source && !sourceIsUuid) params.set('q', filters.source);
  if (filters.cursor) params.set('cursor', filters.cursor);
  return apiGet<AdminRecoveryWorkPageRead>('/api/v1/admin/recovery/work', params, request);
}

export async function fetchAdminRecoveryWorkDetail(
  request: CatalogRequest,
  kind: AdminRecoveryWorkKind,
  workId: string
): Promise<AdminRecoveryWorkRead> {
  return apiGet<AdminRecoveryWorkRead>(
    `/api/v1/admin/recovery/work/${encodeURIComponent(kind)}/${encodeURIComponent(workId)}`,
    new URLSearchParams(),
    request
  );
}

export async function retryAdminRecoveryWork(
  request: CatalogRequest & { body: AdminRecoveryMutationPayload },
  kind: AdminRecoveryWorkKind,
  workId: string
): Promise<AdminRecoveryBatchRead> {
  return apiWrite<AdminRecoveryBatchRead>(
    `/api/v1/admin/recovery/work/${encodeURIComponent(kind)}/${encodeURIComponent(workId)}/retry`,
    'POST',
    request
  );
}

export async function previewAdminRecoveryBatch(
  request: CatalogRequest & { body: AdminRecoveryBatchPreviewPayload }
): Promise<AdminRecoveryBatchRead> {
  return apiWrite<AdminRecoveryBatchRead>('/api/v1/admin/recovery/batches/preview', 'POST', request);
}

export async function fetchAdminRecoveryBatch(
  request: CatalogRequest,
  jobId: string
): Promise<AdminRecoveryBatchRead> {
  return apiGet<AdminRecoveryBatchRead>(
    `/api/v1/admin/recovery/batches/${encodeURIComponent(jobId)}`,
    new URLSearchParams(),
    request
  );
}

export async function scheduleAdminRecoveryBatch(
  request: CatalogRequest & { body: AdminRecoveryBatchMutationPayload },
  jobId: string
): Promise<AdminRecoveryBatchRead> {
  return apiWrite<AdminRecoveryBatchRead>(
    `/api/v1/admin/recovery/batches/${encodeURIComponent(jobId)}/schedule`,
    'POST',
    request
  );
}

export async function cancelAdminRecoveryBatch(
  request: CatalogRequest & { body: AdminRecoveryBatchMutationPayload },
  jobId: string
): Promise<AdminRecoveryBatchRead> {
  return apiWrite<AdminRecoveryBatchRead>(
    `/api/v1/admin/recovery/batches/${encodeURIComponent(jobId)}/cancel`,
    'POST',
    request
  );
}

export async function backfillAdminSourceChannel(
  request: CatalogRequest & { body: AdminSourceBackfillPayload },
  channelId: string
): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>(
    `/api/v1/admin/source-channels/${encodeURIComponent(channelId)}/backfill`,
    'POST',
    request
  );
}

export async function fetchAdminBlockedPerceptualHashes(
  request: CatalogRequest
): Promise<AdminBlockedPerceptualHashRead[]> {
  return apiGet<AdminBlockedPerceptualHashRead[]>('/api/v1/admin/blocked-perceptual-hashes', new URLSearchParams(), request);
}

export async function fetchAdminModerationMemes(request: CatalogRequest, limit = 50): Promise<AdminMemeRead[]> {
  return apiGet<AdminMemeRead[]>('/api/v1/admin/memes', new URLSearchParams({ limit: String(limit) }), request);
}

export async function fetchAdminModerationReports(request: CatalogRequest, limit = 50): Promise<AdminModerationReportRead[]> {
  return apiGet<AdminModerationReportRead[]>('/api/v1/admin/moderation-reports', new URLSearchParams({ limit: String(limit) }), request);
}

export async function fetchAdminModerationDecisions(request: CatalogRequest, limit = 50): Promise<AdminModerationDecisionRead[]> {
  return apiGet<AdminModerationDecisionRead[]>('/api/v1/admin/moderation-decisions', new URLSearchParams({ limit: String(limit) }), request);
}

export async function fetchAdminMemeDetail(request: CatalogRequest, memeId: string): Promise<AdminMemeDetailRead> {
  return apiGet<AdminMemeDetailRead>(`/api/v1/admin/memes/${encodeURIComponent(memeId)}`, new URLSearchParams(), request);
}

export async function fetchAdminMemeTemplates(request: CatalogRequest): Promise<AdminMemeTemplateRead[]> {
  return apiGet<AdminMemeTemplateRead[]>('/api/v1/admin/meme-templates', new URLSearchParams(), request);
}

export async function fetchAdminSeoReviewRows(
  request: CatalogRequest,
  pagination: { limit: number; offset: number }
): Promise<AdminMemeSeoReviewRowRead[]> {
  return apiGet<AdminMemeSeoReviewRowRead[]>(
    '/api/v1/admin/seo-pages',
    new URLSearchParams({ limit: String(pagination.limit), offset: String(pagination.offset) }),
    request
  );
}

export async function fetchAdminTelegramSessions(request: CatalogRequest): Promise<AdminTelegramSessionRead[]> {
  return apiGet<AdminTelegramSessionRead[]>('/api/v1/admin/telegram/sessions', new URLSearchParams(), request);
}

export async function createAdminTelegramSession(request: CatalogRequest & { body: AdminTelegramSessionCreatePayload }): Promise<AdminTelegramSessionRead> {
  return apiWrite<AdminTelegramSessionRead>('/api/v1/admin/telegram/sessions', 'POST', request);
}

export async function updateAdminTelegramSession(
  request: CatalogRequest & { body: AdminTelegramSessionUpdatePayload },
  sessionId: string
): Promise<AdminTelegramSessionRead> {
  return apiWrite<AdminTelegramSessionRead>(
    `/api/v1/admin/telegram/sessions/${encodeURIComponent(sessionId)}`,
    'PATCH',
    request
  );
}

export async function startAdminTelegramQrLogin(
  request: CatalogRequest & { body?: AdminTelegramLoginQrStartPayload }
): Promise<AdminTelegramLoginQrStartRead> {
  return apiWrite<AdminTelegramLoginQrStartRead>(
    '/api/v1/admin/telegram/login-attempts/qr',
    'POST',
    request
  );
}

export async function completeAdminTelegramQrLogin(
  request: CatalogRequest & { body: AdminTelegramLoginQrCompletePayload },
  attemptId: string
): Promise<AdminTelegramLoginQrStatusRead> {
  return apiWrite<AdminTelegramLoginQrStatusRead>(
    `/api/v1/admin/telegram/login-attempts/${encodeURIComponent(attemptId)}/qr/complete`,
    'POST',
    request
  );
}

export async function startAdminTelegramPhoneLogin(
  request: CatalogRequest & { body: AdminTelegramLoginPhoneStartPayload }
): Promise<AdminTelegramLoginPhoneStartRead> {
  return apiWrite<AdminTelegramLoginPhoneStartRead>(
    '/api/v1/admin/telegram/login-attempts/phone',
    'POST',
    request
  );
}

export async function completeAdminTelegramPhoneCodeLogin(
  request: CatalogRequest & { body: AdminTelegramLoginPhoneCodePayload },
  attemptId: string
): Promise<AdminTelegramLoginCompleteRead> {
  return apiWrite<AdminTelegramLoginCompleteRead>(
    `/api/v1/admin/telegram/login-attempts/${encodeURIComponent(attemptId)}/phone/code`,
    'POST',
    request
  );
}

export async function completeAdminTelegramPhonePasswordLogin(
  request: CatalogRequest & { body: AdminTelegramLoginPasswordPayload },
  attemptId: string
): Promise<AdminTelegramLoginCompleteRead> {
  return apiWrite<AdminTelegramLoginCompleteRead>(
    `/api/v1/admin/telegram/login-attempts/${encodeURIComponent(attemptId)}/password`,
    'POST',
    request
  );
}

export async function cancelAdminTelegramLoginAttempt(
  request: CatalogRequest,
  attemptId: string
): Promise<AdminTelegramLoginCancelRead> {
  return apiWrite<AdminTelegramLoginCancelRead>(
    `/api/v1/admin/telegram/login-attempts/${encodeURIComponent(attemptId)}`,
    'DELETE',
    request
  );
}

export async function validateAdminTelegramSession(
  request: CatalogRequest & { body?: AdminTelegramSessionValidatePayload },
  sessionId: string
): Promise<AdminTelegramSessionValidateRead> {
  return apiWrite<AdminTelegramSessionValidateRead>(
    `/api/v1/admin/telegram/sessions/${encodeURIComponent(sessionId)}/validate`,
    'POST',
    request
  );
}

export async function deleteAdminTelegramSession(
  request: CatalogRequest & { body: AdminTelegramSessionDeletePayload },
  sessionId: string
): Promise<AdminTelegramSessionActionRead> {
  return apiWrite<AdminTelegramSessionActionRead>(
    `/api/v1/admin/telegram/sessions/${encodeURIComponent(sessionId)}`,
    'DELETE',
    request
  );
}

export async function fetchAdminTelegramChannels(
  request: CatalogRequest & { telegramSessionId?: string | null; orphaned?: boolean | null }
): Promise<AdminSourceChannelRead[]> {
  const params = new URLSearchParams();
  if (request.telegramSessionId) {
    params.set('telegram_session_id', request.telegramSessionId);
  }
  if (request.orphaned !== undefined && request.orphaned !== null) {
    params.set('orphaned', String(request.orphaned));
  }
  return apiGet<AdminSourceChannelRead[]>('/api/v1/admin/telegram/channels', params, request);
}

export async function fetchAdminTelegramChannelGroups(request: CatalogRequest): Promise<AdminTelegramChannelGroupRead[]> {
  return apiGet<AdminTelegramChannelGroupRead[]>('/api/v1/admin/telegram/channels/grouped', new URLSearchParams(), request);
}

export async function addAdminTelegramChannel(request: CatalogRequest & { body: AdminTelegramChannelCreatePayload }): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>('/api/v1/admin/telegram/channels', 'POST', request);
}

export async function addAdminTelegramChannelFromReference(
  request: CatalogRequest & { body: AdminTelegramChannelFromReferencePayload }
): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>('/api/v1/admin/telegram/channels/from-reference', 'POST', request);
}

export async function updateAdminTelegramChannel(
  request: CatalogRequest & { body: AdminTelegramChannelUpdatePayload },
  channelId: string
): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>(
    `/api/v1/admin/telegram/channels/${encodeURIComponent(channelId)}`,
    'PATCH',
    request
  );
}

export async function assignAdminTelegramChannel(
  request: CatalogRequest & { body: AdminTelegramChannelAssignPayload },
  channelId: string
): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>(
    `/api/v1/admin/telegram/channels/${encodeURIComponent(channelId)}/assign`,
    'POST',
    request
  );
}

export async function orphanAdminTelegramChannel(
  request: CatalogRequest & { body?: AdminTelegramChannelOrphanPayload },
  channelId: string
): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>(
    `/api/v1/admin/telegram/channels/${encodeURIComponent(channelId)}/orphan`,
    'POST',
    request
  );
}

export async function reviewChannelSuggestion(
  request: JsonMutationRequest,
  suggestionId: string,
  decision: 'approve' | 'reject'
): Promise<ChannelSuggestionRead> {
  return apiWrite<ChannelSuggestionRead>(
    `/api/v1/admin/channel-suggestions/${encodeURIComponent(suggestionId)}/${decision}`,
    'POST',
    request
  );
}

export async function addSourceChannel(request: JsonMutationRequest): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>('/api/v1/admin/source-channels', 'POST', request);
}

export async function setSourceChannelPaused(
  request: CatalogRequest,
  channelId: string,
  paused: boolean
): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>(
    `/api/v1/admin/source-channels/${encodeURIComponent(channelId)}/${paused ? 'pause' : 'resume'}`,
    'POST',
    request
  );
}

export async function markSourceChannelDead(
  request: CatalogRequest,
  channelId: string,
  confirmation: string
): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>(
    `/api/v1/admin/source-channels/${encodeURIComponent(channelId)}/mark-dead`,
    'POST',
    { ...request, body: { confirmation } }
  );
}

export async function updateMemeSeoPage(request: JsonMutationRequest, memeId: string): Promise<AdminMemeSeoPageRead> {
  return apiWrite<AdminMemeSeoPageRead>(
    `/api/v1/admin/memes/${encodeURIComponent(memeId)}/seo-page`,
    'PATCH',
    request
  );
}

export async function regenerateMemeSeoPage(request: JsonMutationRequest, memeId: string): Promise<AdminMemeSeoPageRead> {
  return apiWrite<AdminMemeSeoPageRead>(
    `/api/v1/admin/memes/${encodeURIComponent(memeId)}/seo-page/regenerate`,
    'POST',
    request
  );
}

export async function createMemeTemplate(request: JsonMutationRequest): Promise<AdminMemeTemplateRead> {
  return apiWrite<AdminMemeTemplateRead>('/api/v1/admin/meme-templates', 'POST', request);
}

export async function updateMemeTemplate(
  request: JsonMutationRequest,
  templateId: string
): Promise<AdminMemeTemplateRead> {
  return apiWrite<AdminMemeTemplateRead>(
    `/api/v1/admin/meme-templates/${encodeURIComponent(templateId)}`,
    'PATCH',
    request
  );
}

export async function mergeMemeTemplate(
  request: JsonMutationRequest,
  templateId: string
): Promise<AdminMemeTemplateActionRead> {
  return apiWrite<AdminMemeTemplateActionRead>(
    `/api/v1/admin/meme-templates/${encodeURIComponent(templateId)}/merge`,
    'POST',
    request
  );
}

export async function deleteMemeTemplate(
  request: JsonMutationRequest,
  templateId: string
): Promise<AdminMemeTemplateActionRead> {
  return apiWrite<AdminMemeTemplateActionRead>(
    `/api/v1/admin/meme-templates/${encodeURIComponent(templateId)}`,
    'DELETE',
    request
  );
}

export async function createBlockedPerceptualHash(request: JsonMutationRequest): Promise<AdminBlockedPerceptualHashRead> {
  return apiWrite<AdminBlockedPerceptualHashRead>('/api/v1/admin/blocked-perceptual-hashes', 'POST', request);
}

export async function updateBlockedPerceptualHash(
  request: JsonMutationRequest,
  blockedHashId: string
): Promise<AdminBlockedPerceptualHashRead> {
  return apiWrite<AdminBlockedPerceptualHashRead>(
    `/api/v1/admin/blocked-perceptual-hashes/${encodeURIComponent(blockedHashId)}`,
    'PATCH',
    request
  );
}

export async function deactivateBlockedPerceptualHash(
  request: JsonMutationRequest,
  blockedHashId: string
): Promise<AdminBlockedPerceptualHashActionRead> {
  return apiWrite<AdminBlockedPerceptualHashActionRead>(
    `/api/v1/admin/blocked-perceptual-hashes/${encodeURIComponent(blockedHashId)}/deactivate`,
    'POST',
    request
  );
}

export async function deleteBlockedPerceptualHash(
  request: CatalogRequest,
  blockedHashId: string
): Promise<AdminBlockedPerceptualHashActionRead> {
  return apiWrite<AdminBlockedPerceptualHashActionRead>(
    `/api/v1/admin/blocked-perceptual-hashes/${encodeURIComponent(blockedHashId)}`,
    'DELETE',
    request
  );
}

export async function updateMemeModeration(request: JsonMutationRequest, memeId: string): Promise<AdminMemeRead> {
  return apiWrite<AdminMemeRead>(
    `/api/v1/admin/memes/${encodeURIComponent(memeId)}/moderation`,
    'PATCH',
    request
  );
}

export async function deleteAdminMeme(
  request: JsonMutationRequest,
  memeId: string
): Promise<AdminMemeDestructiveActionRead> {
  return apiWrite<AdminMemeDestructiveActionRead>(
    `/api/v1/admin/memes/${encodeURIComponent(memeId)}`,
    'DELETE',
    request
  );
}

export async function mergeAdminMeme(
  request: JsonMutationRequest,
  memeId: string
): Promise<AdminMemeDestructiveActionRead> {
  return apiWrite<AdminMemeDestructiveActionRead>(
    `/api/v1/admin/memes/${encodeURIComponent(memeId)}/merge`,
    'POST',
    request
  );
}

export async function resolveModerationReport(
  request: JsonMutationRequest,
  reportId: string
): Promise<AdminModerationReportRead> {
  return apiWrite<AdminModerationReportRead>(
    `/api/v1/admin/moderation-reports/${encodeURIComponent(reportId)}/resolve`,
    'POST',
    request
  );
}

async function fetchLanding(path: string, request: LandingRequest): Promise<PublicMemeLandingRead> {
  return apiGet<PublicMemeLandingRead>(
    path,
    new URLSearchParams({ limit: String(request.limit), offset: String(request.offset) }),
    request
  );
}

function seoPageParams(request: SeoCatalogPageRequest): URLSearchParams {
  return new URLSearchParams({ limit: String(request.limit), offset: String(request.offset) });
}

function adminAnalyticsRangeParams(request: Pick<AdminAnalyticsRangeRequest, 'startDate' | 'endDate'>): URLSearchParams {
  const params = new URLSearchParams();
  if (request.startDate) params.set('start_date', request.startDate);
  if (request.endDate) params.set('end_date', request.endDate);
  return params;
}

function memePageParams(request: MemePageFilterParams): URLSearchParams {
  const params = new URLSearchParams({
    limit: String(request.limit),
    offset: String(request.offset)
  });

  for (const tag of request.tags ?? []) {
    const normalized = tag.trim();
    if (normalized) {
      params.append('tags', normalized);
    }
  }

  if (request.includeNsfw !== undefined) {
    params.set('include_nsfw', String(request.includeNsfw));
  }

  if (request.mediaType) {
    params.set('media_type', request.mediaType);
  }

  if (request.language) {
    params.set('language', request.language);
  }

  return params;
}

async function apiGet<T>(path: string, params: URLSearchParams, request: CatalogRequest): Promise<T> {
  return apiJson<T>(path, params, request);
}

async function apiJson<T>(
  path: string,
  params: URLSearchParams | undefined,
  request: CatalogRequest,
  init: RequestInit = {}
): Promise<T> {
  const url = new URL(path, request.baseUrl);
  if (params) {
    url.search = params.toString();
  }

  const headers = new Headers(init.headers);
  headers.set('accept', 'application/json');
  if (isUnsafeMethod(init.method)) {
    headers.set('x-requested-with', 'XMLHttpRequest');
  }
  if (request.cookieHeader) {
    headers.set('cookie', request.cookieHeader);
  }

  const response = await request.fetch(url, { ...init, headers });
  request.onResponse?.(response);
  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(response.status, readErrorDetail(payload) ?? `Catalog API returned ${response.status}`);
  }

  return payload as T;
}

function isUnsafeMethod(method: string | undefined): boolean {
  const normalizedMethod = (method ?? 'GET').toUpperCase();
  return normalizedMethod !== 'GET' && normalizedMethod !== 'HEAD' && normalizedMethod !== 'OPTIONS';
}

async function apiMutation<T>(path: string, method: 'DELETE' | 'POST', request: MemeActionRequest): Promise<T> {
  const headers = new Headers({ accept: 'application/json', 'x-requested-with': 'XMLHttpRequest' });
  const body = method === 'POST' ? request.body : undefined;
  if (body !== undefined) {
    headers.set('content-type', 'application/json');
  }
  if (request.cookieHeader) {
    headers.set('cookie', request.cookieHeader);
  }

  const response = await request.fetch(buildApiInput(path, request.baseUrl), {
    method,
    headers,
    credentials: 'include',
    keepalive: request.keepalive,
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  request.onResponse?.(response);
  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(response.status, readErrorDetail(payload) ?? `Meme action returned ${response.status}`);
  }

  return payload as T;
}

async function apiWrite<T>(path: string, method: 'DELETE' | 'PATCH' | 'POST' | 'PUT', request: JsonMutationRequest): Promise<T> {
  const url = new URL(path, request.baseUrl);
  const headers = new Headers({ accept: 'application/json', 'x-requested-with': 'XMLHttpRequest' });
  if (request.body !== undefined) {
    headers.set('content-type', 'application/json');
  }
  if (request.cookieHeader) {
    headers.set('cookie', request.cookieHeader);
  }

  const response = await request.fetch(url, {
    method,
    headers,
    body: request.body === undefined ? undefined : JSON.stringify(request.body)
  });
  request.onResponse?.(response);
  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(
      response.status,
      readErrorDetail(payload) ?? `API returned ${response.status}`,
      isRecord(payload) && isRecord(payload.detail) ? payload.detail : undefined
    );
  }

  return payload as T;
}

async function apiBrowserWrite<T>(
  path: string,
  method: 'PATCH' | 'POST' | 'PUT',
  request: PreferenceMutationRequest
): Promise<T> {
  const headers = new Headers({
    accept: 'application/json',
    'content-type': 'application/json',
    'x-requested-with': 'XMLHttpRequest'
  });
  if (request.cookieHeader) {
    headers.set('cookie', request.cookieHeader);
  }

  const response = await request.fetch(buildApiInput(path, request.baseUrl), {
    method,
    headers,
    credentials: 'include',
    body: JSON.stringify(request.body)
  });
  request.onResponse?.(response);
  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(response.status, readErrorDetail(payload) ?? `API write returned ${response.status}`);
  }

  return payload as T;
}

async function apiJsonWrite<T>(
  path: string,
  method: 'PATCH' | 'POST',
  request: MemeActionRequest,
  body: unknown
): Promise<T> {
  const headers = new Headers({
    accept: 'application/json',
    'content-type': 'application/json',
    'x-requested-with': 'XMLHttpRequest'
  });
  if (request.cookieHeader) {
    headers.set('cookie', request.cookieHeader);
  }

  const response = await request.fetch(buildApiInput(path, request.baseUrl), {
    method,
    headers,
    credentials: 'include',
    body: JSON.stringify(body)
  });
  request.onResponse?.(response);
  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(response.status, readErrorDetail(payload) ?? `API write returned ${response.status}`);
  }

  return payload as T;
}

function buildApiInput(path: string, baseUrl: string | undefined): RequestInfo | URL {
  return baseUrl ? new URL(path, baseUrl) : path;
}

function readActionAttribution(body: unknown): unknown | undefined {
  if (!body || typeof body !== 'object' || !('attribution' in body)) {
    return undefined;
  }
  return body.attribution;
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function readErrorDetail(payload: unknown): string | null {
  if (!isRecord(payload)) return null;

  const detail = payload.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return formatValidationDetails(detail);
  if (isRecord(detail) && typeof detail.message === 'string') return detail.message;

  return null;
}

const MAX_VALIDATION_ERRORS = 3;
const MAX_VALIDATION_FIELD_MESSAGE_LENGTH = 120;
const MAX_VALIDATION_DETAIL_LENGTH = 360;

function formatValidationDetails(details: unknown[]): string | null {
  const messages = details
    .slice(0, MAX_VALIDATION_ERRORS)
    .map(formatValidationDetail)
    .filter((message): message is string => message !== null);
  if (!messages.length) return null;

  const remaining = details.length - MAX_VALIDATION_ERRORS;
  const suffix = remaining > 0 ? `; +${remaining} more` : '';
  return truncateValidationMessage(`${messages.join('; ')}${suffix}`, MAX_VALIDATION_DETAIL_LENGTH);
}

function formatValidationDetail(detail: unknown): string | null {
  if (!isRecord(detail) || typeof detail.msg !== 'string') return null;

  const location = Array.isArray(detail.loc)
    ? detail.loc
        .filter((part): part is string | number => typeof part === 'string' || typeof part === 'number')
        .filter((part) => part !== 'body' && part !== 'query')
        .join('.')
    : '';
  const message = truncateValidationMessage(detail.msg, MAX_VALIDATION_FIELD_MESSAGE_LENGTH);
  return location ? `${location}: ${message}` : message;
}

function truncateValidationMessage(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, Math.max(0, maxLength - 1))}…` : value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function emptyMemePage(limit: number, offset: number): PublicMemeSearchPageRead {
  return {
    items: [],
    limit,
    offset,
    total: 0,
    has_more: false,
    request_id: 'req_empty'
  };
}

export function emptyTrendPage(limit: number, offset: number): PublicMemeTrendPageRead {
  return {
    items: [],
    limit,
    offset,
    total: 0,
    has_more: false,
    request_id: 'req_empty'
  };
}

export function emptyTrendTimeline(granularity: 'month' | 'year', limit: number, offset: number): PublicTrendTimelinePageRead {
  return {
    granularity,
    periods: [],
    limit,
    offset,
    total: 0,
    has_more: false
  };
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}
