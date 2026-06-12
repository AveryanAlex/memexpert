export const MAX_TREND_COMPARE_ITEMS = 6;

export function readComparisonItems(params: URLSearchParams): string[] {
  return params
    .getAll('item')
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, MAX_TREND_COMPARE_ITEMS);
}

export function comparisonHref(items: string[]): string {
  const params = new URLSearchParams();
  for (const item of items) {
    const normalized = item.trim();
    if (normalized) {
      params.append('item', normalized);
    }
  }
  const query = params.toString();
  return query ? `/trends/compare?${query}` : '/trends/compare';
}

export function readTimelineGranularity(raw: string | null): 'month' | 'year' {
  return raw === 'year' ? 'year' : 'month';
}

export function trendTimelineHref(granularity: 'month' | 'year', offset = 0): string {
  const params = new URLSearchParams({ granularity });
  if (offset > 0) {
    params.set('offset', String(offset));
  }
  return `/trends/timeline?${params.toString()}`;
}
