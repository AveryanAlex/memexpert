import type {
  AdminMemeDetailRead,
  AdminMemeRead,
  AdminMemeTemplateRead,
  AdminModerationDecisionRead,
  AdminModerationReportRead,
  AdminSessionRead,
  AdminSourceChannelRead,
  ChannelSuggestionRead,
  CurrentSessionRead,
  PublicMemeDetailRead,
  PublicMemeLandingRead,
  PublicMemePopularitySummaryRead,
  PublicMemeSearchPageRead,
  PublicMemeTrendPageRead,
  PublicTrendSummaryRead,
  MemeReportRead,
  ModerationReason,
  TelegramLinkStartRead
} from './types';

export const DEFAULT_PAGE_SIZE = 12;

export type ApiFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface CatalogRequest {
  fetch: ApiFetch;
  baseUrl: string;
  cookieHeader?: string;
  onResponse?: (response: Response) => void;
}

interface PageRequest extends CatalogRequest {
  query: string;
  limit: number;
  offset: number;
}

interface DetailRequest extends CatalogRequest {
  memeId: string;
}

interface TrendRequest extends CatalogRequest {
  ranking?: 'trending' | 'fastest_rising' | 'most_liked';
  limit: number;
  offset: number;
}

interface MemeActionRequest {
  fetch: ApiFetch;
  baseUrl?: string;
  cookieHeader?: string;
  onResponse?: (response: Response) => void;
  memeId: string;
}

interface MemeReportRequest extends MemeActionRequest {
  reason: ModerationReason;
  note?: string | null;
}

export interface RemoveActionResponse {
  removed?: boolean;
}

interface AdminMutationRequest extends CatalogRequest {
  body?: unknown;
}

interface LandingRequest extends CatalogRequest {
  slug: string;
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
  const params = new URLSearchParams({
    limit: String(request.limit),
    offset: String(request.offset)
  });

  if (query) {
    params.set('query', query);
    return apiGet<PublicMemeSearchPageRead>('/api/v1/memes/search', params, request);
  }

  return apiGet<PublicMemeSearchPageRead>('/api/v1/memes/browse', params, request);
}

export async function fetchCurrentSession(request: CatalogRequest): Promise<CurrentSessionRead> {
  return apiJson<CurrentSessionRead>('/api/v1/auth/session', undefined, request);
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
  return apiGet<PublicMemeDetailRead>(
    path,
    new URLSearchParams({ include_nsfw: 'false' }),
    request
  );
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

export async function fetchMemePopularitySummary(request: DetailRequest): Promise<PublicMemePopularitySummaryRead> {
  return apiGet<PublicMemePopularitySummaryRead>(
    `/api/v1/memes/${encodeURIComponent(request.memeId)}/popularity`,
    new URLSearchParams({ include_nsfw: 'false' }),
    request
  );
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

export async function reportMeme(request: MemeReportRequest): Promise<MemeReportRead> {
  return apiJsonWrite<MemeReportRead>(`/api/v1/memes/${encodeURIComponent(request.memeId)}/report`, 'POST', request, {
    reason: request.reason,
    note: request.note ?? null
  });
}

export async function fetchTagLanding(request: LandingRequest): Promise<PublicMemeLandingRead> {
  return fetchLanding(`/api/v1/memes/tags/${encodeURIComponent(request.slug)}`, request);
}

export async function fetchTemplateLanding(request: LandingRequest): Promise<PublicMemeLandingRead> {
  return fetchLanding(`/api/v1/memes/templates/${encodeURIComponent(request.slug)}`, request);
}

export async function fetchAdminSession(request: CatalogRequest): Promise<AdminSessionRead> {
  return apiGet<AdminSessionRead>('/api/v1/admin/session', new URLSearchParams(), request);
}

export async function fetchAdminDashboard(request: CatalogRequest): Promise<{
  suggestions: ChannelSuggestionRead[];
  sourceChannels: AdminSourceChannelRead[];
  templates: AdminMemeTemplateRead[];
  memes: AdminMemeRead[];
  reports: AdminModerationReportRead[];
  decisions: AdminModerationDecisionRead[];
}> {
  const [suggestions, sourceChannels, templates, memes, reports, decisions] = await Promise.all([
    apiGet<ChannelSuggestionRead[]>('/api/v1/admin/channel-suggestions', new URLSearchParams(), request),
    apiGet<AdminSourceChannelRead[]>('/api/v1/admin/source-channels', new URLSearchParams(), request),
    apiGet<AdminMemeTemplateRead[]>('/api/v1/admin/meme-templates', new URLSearchParams(), request),
    apiGet<AdminMemeRead[]>('/api/v1/admin/memes', new URLSearchParams({ limit: '20' }), request),
    apiGet<AdminModerationReportRead[]>('/api/v1/admin/moderation-reports', new URLSearchParams({ limit: '20' }), request),
    apiGet<AdminModerationDecisionRead[]>('/api/v1/admin/moderation-decisions', new URLSearchParams({ limit: '20' }), request)
  ]);

  return { suggestions, sourceChannels, templates, memes, reports, decisions };
}

export async function fetchAdminMemeDetail(request: CatalogRequest, memeId: string): Promise<AdminMemeDetailRead> {
  return apiGet<AdminMemeDetailRead>(`/api/v1/admin/memes/${encodeURIComponent(memeId)}`, new URLSearchParams(), request);
}

export async function fetchAdminMemeTemplates(request: CatalogRequest): Promise<AdminMemeTemplateRead[]> {
  return apiGet<AdminMemeTemplateRead[]>('/api/v1/admin/meme-templates', new URLSearchParams(), request);
}

export async function reviewChannelSuggestion(
  request: AdminMutationRequest,
  suggestionId: string,
  decision: 'approve' | 'reject'
): Promise<ChannelSuggestionRead> {
  return apiWrite<ChannelSuggestionRead>(
    `/api/v1/admin/channel-suggestions/${encodeURIComponent(suggestionId)}/${decision}`,
    'POST',
    request
  );
}

export async function addSourceChannel(request: AdminMutationRequest): Promise<AdminSourceChannelRead> {
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

export async function updateMemeTemplate(
  request: AdminMutationRequest,
  templateId: string
): Promise<AdminMemeTemplateRead> {
  return apiWrite<AdminMemeTemplateRead>(
    `/api/v1/admin/meme-templates/${encodeURIComponent(templateId)}`,
    'PATCH',
    request
  );
}

export async function updateMemeModeration(request: AdminMutationRequest, memeId: string): Promise<AdminMemeRead> {
  return apiWrite<AdminMemeRead>(
    `/api/v1/admin/memes/${encodeURIComponent(memeId)}/moderation`,
    'PATCH',
    request
  );
}

export async function resolveModerationReport(
  request: AdminMutationRequest,
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
  const headers = new Headers({ accept: 'application/json' });
  if (request.cookieHeader) {
    headers.set('cookie', request.cookieHeader);
  }

  const response = await request.fetch(buildApiInput(path, request.baseUrl), {
    method,
    headers,
    credentials: 'include'
  });
  request.onResponse?.(response);
  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(response.status, readErrorDetail(payload) ?? `Meme action returned ${response.status}`);
  }

  return payload as T;
}

async function apiWrite<T>(path: string, method: 'PATCH' | 'POST', request: AdminMutationRequest): Promise<T> {
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
  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(response.status, readErrorDetail(payload) ?? `Admin API returned ${response.status}`);
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
    has_more: false
  };
}

export function emptyTrendPage(limit: number, offset: number): PublicMemeTrendPageRead {
  return {
    items: [],
    limit,
    offset,
    total: 0,
    has_more: false
  };
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}
