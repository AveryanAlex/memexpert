import type { AdminAnalyticsRangeRead } from '$lib/api/types';

export const ADMIN_ANALYTICS_PRESETS = [
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days' },
  { days: 365, label: '365 days' }
] as const;

export interface AdminAnalyticsRangeParams {
  startDate: string | null;
  endDate: string | null;
}

export interface AdminAnalyticsResolvedRange {
  startDate: string;
  endDate: string;
  comparisonStartDate?: string;
  comparisonEndDate?: string;
  timezone?: string;
  bucket?: string;
}

export function analyticsRangeParamsFromUrl(url: URL): AdminAnalyticsRangeParams {
  return {
    startDate: cleanIsoDate(url.searchParams.get('start_date')),
    endDate: cleanIsoDate(url.searchParams.get('end_date'))
  };
}

export function analyticsRangeFromRead(range: AdminAnalyticsRangeRead | null | undefined): AdminAnalyticsResolvedRange | null {
  if (!range) return null;
  return {
    startDate: range.start_date,
    endDate: range.end_date,
    comparisonStartDate: range.comparison_start_date,
    comparisonEndDate: range.comparison_end_date,
    timezone: range.timezone,
    bucket: range.bucket
  };
}

export function analyticsRangeForControls(
  resolvedRange: AdminAnalyticsResolvedRange | null,
  requestedRange: AdminAnalyticsRangeParams
): AdminAnalyticsRangeParams {
  return {
    startDate: resolvedRange?.startDate ?? requestedRange.startDate,
    endDate: resolvedRange?.endDate ?? requestedRange.endDate
  };
}

export function adminAnalyticsHref(
  path: string,
  range: AdminAnalyticsResolvedRange | AdminAnalyticsRangeParams | null | undefined,
  additional: Record<string, string | number | null | undefined> = {}
): string {
  const params = new URLSearchParams();
  const startDate = range && 'startDate' in range ? range.startDate : null;
  const endDate = range && 'endDate' in range ? range.endDate : null;
  if (startDate) params.set('start_date', startDate);
  if (endDate) params.set('end_date', endDate);

  for (const [key, value] of Object.entries(additional)) {
    if (value !== null && value !== undefined && String(value) !== '') {
      params.set(key, String(value));
    }
  }

  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

export function adminAnalyticsPresetHref(path: string, days: number): string {
  const endDate = utcDateString(new Date());
  const startDate = addUtcDays(endDate, 1 - days);
  return adminAnalyticsHref(path, { startDate, endDate });
}

export function analyticsRangeLabel(range: AdminAnalyticsResolvedRange | null | undefined): string {
  if (!range) return 'Last 30 days · UTC';
  return `${formatUtcDate(range.startDate)} – ${formatUtcDate(range.endDate)} · ${range.timezone ?? 'UTC'}`;
}

export function formatUtcDate(value: string | null | undefined): string {
  if (!value) return 'Not available';
  const parsed = new Date(`${value.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat('en', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC'
  }).format(parsed);
}

export function dateDurationDays(range: Pick<AdminAnalyticsResolvedRange, 'startDate' | 'endDate'> | null | undefined): number | null {
  if (!range) return null;
  const start = Date.parse(`${range.startDate}T00:00:00Z`);
  const end = Date.parse(`${range.endDate}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return Math.floor((end - start) / 86_400_000) + 1;
}

function cleanIsoDate(value: string | null): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(parsed) && utcDateString(new Date(parsed)) === value ? value : null;
}

function addUtcDays(date: string, days: number): string {
  const parsed = new Date(`${date}T00:00:00Z`);
  parsed.setUTCDate(parsed.getUTCDate() + days);
  return utcDateString(parsed);
}

function utcDateString(date: Date): string {
  return date.toISOString().slice(0, 10);
}
