export const MAX_TREND_COMPARE_ITEMS = 6;

export function readComparisonItems(params: URLSearchParams): string[] {
  return uniqueComparisonItems(params.getAll('item')).slice(0, MAX_TREND_COMPARE_ITEMS);
}

export function comparisonHref(items: string[]): string {
  const params = new URLSearchParams();
  for (const item of uniqueComparisonItems(items).slice(0, MAX_TREND_COMPARE_ITEMS)) {
    params.append('item', item);
  }
  const query = params.toString();
  return query ? `/trends/compare?${query}` : '/trends/compare';
}

function uniqueComparisonItems(items: Iterable<string>): string[] {
  const normalizedItems: string[] = [];
  const seen = new Set<string>();

  for (const item of items) {
    const normalized = item.trim();
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    normalizedItems.push(normalized);
  }

  return normalizedItems;
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
