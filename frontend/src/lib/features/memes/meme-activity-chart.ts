import type {
  PublicMemeActivityPointRead,
  PublicMemeAnalyticsGranularity,
  PublicMemeObservedSourcePointRead
} from '$lib/api/types';

const DAY_MS = 86_400_000;

export type ObservedTelegramMetric = 'views' | 'reactions' | 'comments' | 'reposts';

export interface MemeActivityChartDatum {
  observedMs: number;
  bucketStart: string;
  bucketEnd: string;
  granularity: PublicMemeAnalyticsGranularity;
  durationDays: number;
  activityPerDay: number;
  sourcesPerDay: number;
  memeExpertPerDay: number;
  rawActivity: number;
  rawSources: number;
  rawMemeExpert: number;
}

export interface ObservedTelegramChartDatum {
  observedMs: number;
  observedAt: string;
  value: number | null;
}

export interface KnownObservedTelegramChartDatum extends ObservedTelegramChartDatum {
  value: number;
}

export function memeActivityChartDatum(point: PublicMemeActivityPointRead): MemeActivityChartDatum | null {
  const observedMs = Date.parse(point.bucket_start);
  if (!Number.isFinite(observedMs)) return null;

  const durationDays = activityBucketDurationDays(point.bucket_start, point.bucket_end);
  const rawSources = point.source_views + point.source_reactions + point.source_reposts;
  const rawMemeExpert =
    point.memeexpert_views +
    point.memeexpert_sends +
    point.memeexpert_saves +
    point.memeexpert_favorites;

  return {
    observedMs,
    bucketStart: point.bucket_start,
    bucketEnd: point.bucket_end,
    granularity: point.granularity,
    durationDays,
    activityPerDay: point.recorded_activity / durationDays,
    sourcesPerDay: rawSources / durationDays,
    memeExpertPerDay: rawMemeExpert / durationDays,
    rawActivity: point.recorded_activity,
    rawSources,
    rawMemeExpert
  };
}

export function activityBucketDurationDays(bucketStart: string, bucketEnd: string): number {
  const start = Date.parse(bucketStart);
  const end = Date.parse(bucketEnd);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 1;
  return Math.max(1, (end - start) / DAY_MS);
}

export function canPlotMemeActivity(points: readonly MemeActivityChartDatum[]): boolean {
  return points.length >= 2 && points.some((point) => point.rawActivity > 0);
}

export function observedTelegramLineData(
  points: readonly PublicMemeObservedSourcePointRead[],
  metric: ObservedTelegramMetric
): ObservedTelegramChartDatum[] {
  return points.flatMap((point) => {
    const observedMs = Date.parse(point.observed_at);
    return Number.isFinite(observedMs)
      ? [{ observedMs, observedAt: point.observed_at, value: point[metric] }]
      : [];
  });
}

export function knownObservedTelegramPoints(
  points: readonly ObservedTelegramChartDatum[]
): KnownObservedTelegramChartDatum[] {
  return points.filter((point): point is KnownObservedTelegramChartDatum => point.value !== null);
}
