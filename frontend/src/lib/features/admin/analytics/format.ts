import type {
  AdminAnalyticsBreakdownRead,
  AdminAnalyticsMetricRead,
  AdminAnalyticsSurfaceRead
} from '$lib/api/types';

const numberFormatter = new Intl.NumberFormat('en-US');
const compactFormatter = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

export type AnalyticsMetricFormat = 'count' | 'percent' | 'milliseconds';

export interface AnalyticsCategoryDatum {
  label: string;
  count: number;
}

export function metricOrZero(
  metrics: Record<string, AdminAnalyticsMetricRead>,
  key: string
): AdminAnalyticsMetricRead {
  return metrics[key] ?? { value: 0, previous_value: 0, change: 0, change_percent: null };
}

export function formatAnalyticsNumber(value: number | null | undefined, format: AnalyticsMetricFormat = 'count'): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Not available';
  const safeValue = finiteNonNegative(value);
  if (format === 'percent') return `${safeValue.toFixed(safeValue >= 10 ? 0 : 1)}%`;
  if (format === 'milliseconds') return `${numberFormatter.format(Math.round(safeValue))} ms`;
  return numberFormatter.format(safeValue);
}

export function compactAnalyticsNumber(value: number | null | undefined): string {
  return compactFormatter.format(finiteNonNegative(value));
}

export function formatMetricChange(metric: AdminAnalyticsMetricRead, format: AnalyticsMetricFormat = 'count'): string {
  const change = Number.isFinite(metric.change) ? metric.change : 0;
  const direction = change > 0 ? '+' : change < 0 ? '-' : '';
  const absolute = formatAnalyticsNumber(Math.abs(change), format);
  if (metric.change_percent !== null && Number.isFinite(metric.change_percent)) {
    const percent = Math.abs(metric.change_percent).toFixed(Math.abs(metric.change_percent) >= 10 ? 0 : 1);
    return `${direction}${absolute} (${direction}${percent}%) vs. prior period`;
  }
  if (metric.previous_value === 0 && metric.value > 0) return `${direction}${absolute} (new) vs. prior period`;
  return `${direction}${absolute} vs. prior period`;
}

export function metricChangeTone(metric: AdminAnalyticsMetricRead, inverse = false): 'neutral' | 'positive' | 'negative' {
  if (metric.change === 0) return 'neutral';
  const positive = inverse ? metric.change < 0 : metric.change > 0;
  return positive ? 'positive' : 'negative';
}

export function breakdownLabel(item: AdminAnalyticsBreakdownRead | AdminAnalyticsSurfaceRead): string {
  const candidateKeys = ['label', 'key', 'surface', 'event_type', 'media_type', 'language', 'visibility', 'status', 'state', 'name', 'kind'];
  const record = item as unknown as Record<string, unknown>;
  for (const key of candidateKeys) {
    const value = record[key];
    if (typeof value === 'string' && value.trim()) return humanizeAnalyticsValue(value);
  }
  return 'Not classified';
}

export function breakdownCount(item: AdminAnalyticsBreakdownRead | AdminAnalyticsSurfaceRead): number {
  return finiteNonNegative(item.count);
}

export function aggregateAnalyticsCategories(
  items: Array<AdminAnalyticsBreakdownRead | AdminAnalyticsSurfaceRead>,
  limit: number
): { items: AnalyticsCategoryDatum[]; aggregated: boolean } {
  const countsByLabel = new Map<string, number>();
  for (const item of items) {
    const label = breakdownLabel(item);
    countsByLabel.set(label, (countsByLabel.get(label) ?? 0) + breakdownCount(item));
  }

  const sorted = Array.from(countsByLabel, ([label, count]) => ({ label, count })).sort(
    (left, right) => right.count - left.count || left.label.localeCompare(right.label)
  );
  const boundedLimit = Math.max(1, Math.floor(limit));
  if (sorted.length <= boundedLimit) return { items: sorted, aggregated: false };

  const retained = sorted.filter((item) => item.label !== 'Other').slice(0, boundedLimit - 1);
  const retainedLabels = new Set(retained.map((item) => item.label));
  const otherCount = sorted.reduce(
    (sum, item) => sum + (retainedLabels.has(item.label) ? 0 : item.count),
    0
  );
  return {
    items: [...retained, { label: 'Other', count: otherCount }],
    aggregated: true
  };
}

export function recordNumber(record: Record<string, unknown>, keys: string[]): number {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'number' && Number.isFinite(value)) return Math.max(value, 0);
  }
  return 0;
}

export function recordText(record: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return null;
}

export function humanizeAnalyticsValue(value: string | null | undefined): string {
  if (!value) return 'Not available';
  return value
    .replaceAll('_', ' ')
    .replaceAll('-', ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function finiteNonNegative(value: number | null | undefined): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(value, 0) : 0;
}
