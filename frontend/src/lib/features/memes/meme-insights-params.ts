import type { PublicMemeAnalyticsWindow, PublicMemeSourceSort } from '$lib/api/types';

export const MEME_SOURCE_PAGE_SIZE = 10;
export const DEFAULT_MEME_SOURCE_SORT: PublicMemeSourceSort = 'views_desc';
export const DEFAULT_MEME_ANALYTICS_WINDOW: PublicMemeAnalyticsWindow = '30d';
export const MEME_SOURCES_ANCHOR = 'meme-sources-activity';
export const MEME_ANALYTICS_ANCHOR = 'meme-professional-analytics';

export const MEME_SOURCE_SORTS: ReadonlyArray<{ value: PublicMemeSourceSort; label: string }> = [
  { value: 'views_desc', label: 'Most viewed' },
  { value: 'reactions_desc', label: 'Most reactions' },
  { value: 'reposts_desc', label: 'Most reposts' },
  { value: 'interaction_rate_desc', label: 'Best interaction rate' },
  { value: 'newest', label: 'Newest' },
  { value: 'oldest', label: 'Oldest' }
];

export const MEME_ANALYTICS_WINDOWS: ReadonlyArray<{ value: PublicMemeAnalyticsWindow; label: string }> = [
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: '90d', label: '90 days' },
  { value: 'all', label: 'All' }
];

export interface MemeInsightsParams {
  sourceSort: PublicMemeSourceSort;
  sourceOffset: number;
  sourceSnapshot: string | null;
  analyticsWindow: PublicMemeAnalyticsWindow;
}

interface MemeInsightsParamChanges {
  sourceSort?: PublicMemeSourceSort;
  sourceOffset?: number;
  sourceSnapshot?: string | null;
  analyticsWindow?: PublicMemeAnalyticsWindow;
}

const sourceSortValues = new Set<PublicMemeSourceSort>(MEME_SOURCE_SORTS.map((item) => item.value));
const analyticsWindowValues = new Set<PublicMemeAnalyticsWindow>(MEME_ANALYTICS_WINDOWS.map((item) => item.value));

export function parseMemeInsightsParams(searchParams: URLSearchParams): MemeInsightsParams {
  return {
    sourceSort: parseSourceSort(searchParams.get('source_sort')),
    sourceOffset: parseOffset(searchParams.get('source_offset')),
    sourceSnapshot: parseSnapshot(searchParams.get('source_snapshot')),
    analyticsWindow: parseAnalyticsWindow(searchParams.get('activity_window'))
  };
}

export function memeInsightsHref(
  pathname: string,
  currentSearchParams: URLSearchParams,
  changes: MemeInsightsParamChanges
): string {
  const params = new URLSearchParams(currentSearchParams);
  setDefaultableParam(params, 'source_sort', changes.sourceSort, DEFAULT_MEME_SOURCE_SORT);
  setOffsetParam(params, changes.sourceOffset);
  setNullableParam(params, 'source_snapshot', changes.sourceSnapshot);
  setDefaultableParam(params, 'activity_window', changes.analyticsWindow, DEFAULT_MEME_ANALYTICS_WINDOW);
  const query = params.toString();
  const anchor = changes.analyticsWindow === undefined ? MEME_SOURCES_ANCHOR : MEME_ANALYTICS_ANCHOR;
  return `${query ? `${pathname}?${query}` : pathname}#${anchor}`;
}

function parseSourceSort(value: string | null): PublicMemeSourceSort {
  return value && sourceSortValues.has(value as PublicMemeSourceSort)
    ? (value as PublicMemeSourceSort)
    : DEFAULT_MEME_SOURCE_SORT;
}

function parseAnalyticsWindow(value: string | null): PublicMemeAnalyticsWindow {
  return value && analyticsWindowValues.has(value as PublicMemeAnalyticsWindow)
    ? (value as PublicMemeAnalyticsWindow)
    : DEFAULT_MEME_ANALYTICS_WINDOW;
}

function parseOffset(value: string | null): number {
  if (!value || !/^\d+$/.test(value)) return 0;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : 0;
}

function parseSnapshot(value: string | null): string | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : null;
}

function setDefaultableParam<T extends string>(
  params: URLSearchParams,
  key: string,
  value: T | undefined,
  defaultValue: T
) {
  if (value === undefined) return;
  if (value === defaultValue) params.delete(key);
  else params.set(key, value);
}

function setOffsetParam(params: URLSearchParams, value: number | undefined) {
  if (value === undefined) return;
  if (value <= 0) params.delete('source_offset');
  else params.set('source_offset', String(value));
}

function setNullableParam(params: URLSearchParams, key: string, value: string | null | undefined) {
  if (value === undefined) return;
  if (value === null) params.delete(key);
  else params.set(key, value);
}
