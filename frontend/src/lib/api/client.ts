import type {
  AdminBlockedPerceptualHashActionRead,
  AdminBlockedPerceptualHashRead,
  AdminMemeDestructiveActionRead,
  AdminMemeDetailRead,
  AdminMemeRead,
  AdminMemeTemplateActionRead,
  AdminMemeTemplateRead,
  AdminModerationDecisionRead,
  AdminModerationReportRead,
  AdminSessionRead,
  AdminSourceChannelRead,
  ChannelSuggestionRead,
  CollectionInviteLinkRead,
  CollectionInviteRead,
  CollectionMemberRead,
  CollectionMembershipRole,
  CollectionVisibility,
  ContentKind,
  ContentLanguage,
  CurrentSessionRead,
  MemeLibraryRead,
  MemeSearchScope,
  PinnedMemeRead,
  ProfileStatsRead,
  PublicMemeDetailRead,
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
    message: string
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

export async function fetchCurrentSession(request: CatalogRequest): Promise<CurrentSessionRead> {
  return apiJson<CurrentSessionRead>('/api/v1/auth/session', undefined, request);
}

export async function fetchProfileStats(request: CatalogRequest): Promise<ProfileStatsRead> {
  return apiGet<ProfileStatsRead>('/api/v1/auth/profile-stats', new URLSearchParams(), request);
}

export async function refreshCurrentSession(request: CatalogRequest): Promise<CurrentSessionRead> {
  return apiJson<CurrentSessionRead>('/api/v1/auth/session/refresh', undefined, request, { method: 'POST' });
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

export async function saveMemeToCollection(request: CatalogRequest & { collectionId: string; memeId: string }): Promise<SaveCollectionMemeResponse> {
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
export async function favoriteMeme(request: MemeActionRequest): Promise<unknown> {
  return apiMutation(`/api/v1/memes/${encodeURIComponent(request.memeId)}/favorite`, 'POST', request);
}

export async function unfavoriteMeme(request: MemeActionRequest): Promise<RemoveActionResponse> {
  return apiMutation(`/api/v1/memes/${encodeURIComponent(request.memeId)}/favorite`, 'DELETE', request);
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

export async function fetchAdminDashboard(request: CatalogRequest): Promise<{
  suggestions: ChannelSuggestionRead[];
  sourceChannels: AdminSourceChannelRead[];
  templates: AdminMemeTemplateRead[];
  blockedPerceptualHashes: AdminBlockedPerceptualHashRead[];
  memes: AdminMemeRead[];
  reports: AdminModerationReportRead[];
  decisions: AdminModerationDecisionRead[];
}> {
  const [suggestions, sourceChannels, templates, blockedPerceptualHashes, memes, reports, decisions] = await Promise.all([
    apiGet<ChannelSuggestionRead[]>('/api/v1/admin/channel-suggestions', new URLSearchParams(), request),
    apiGet<AdminSourceChannelRead[]>('/api/v1/admin/source-channels', new URLSearchParams(), request),
    apiGet<AdminMemeTemplateRead[]>('/api/v1/admin/meme-templates', new URLSearchParams(), request),
    apiGet<AdminBlockedPerceptualHashRead[]>('/api/v1/admin/blocked-perceptual-hashes', new URLSearchParams(), request),
    apiGet<AdminMemeRead[]>('/api/v1/admin/memes', new URLSearchParams({ limit: '20' }), request),
    apiGet<AdminModerationReportRead[]>('/api/v1/admin/moderation-reports', new URLSearchParams({ limit: '20' }), request),
    apiGet<AdminModerationDecisionRead[]>('/api/v1/admin/moderation-decisions', new URLSearchParams({ limit: '20' }), request)
  ]);

  return { suggestions, sourceChannels, templates, blockedPerceptualHashes, memes, reports, decisions };
}

export async function fetchAdminMemeDetail(request: CatalogRequest, memeId: string): Promise<AdminMemeDetailRead> {
  return apiGet<AdminMemeDetailRead>(`/api/v1/admin/memes/${encodeURIComponent(memeId)}`, new URLSearchParams(), request);
}

export async function fetchAdminMemeTemplates(request: CatalogRequest): Promise<AdminMemeTemplateRead[]> {
  return apiGet<AdminMemeTemplateRead[]>('/api/v1/admin/meme-templates', new URLSearchParams(), request);
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
  channelId: string
): Promise<AdminSourceChannelRead> {
  return apiWrite<AdminSourceChannelRead>(
    `/api/v1/admin/source-channels/${encodeURIComponent(channelId)}/mark-dead`,
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

  const headers = new Headers({ accept: 'application/json' });
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
    throw new ApiError(response.status, readErrorDetail(payload) ?? `API returned ${response.status}`);
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
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = payload.detail;
    return typeof detail === 'string' ? detail : null;
  }

  return null;
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
