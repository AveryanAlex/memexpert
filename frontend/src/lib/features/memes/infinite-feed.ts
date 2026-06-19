import type { ContentKind, ContentLanguage, MemeSearchScope, PublicMemeSearchPageRead, PublicMemeSearchResultRead } from '$lib/api/types';

export interface MemeFeedFilters {
  query: string;
  tags?: string[];
  includeNsfw?: boolean;
  mediaType?: ContentKind | null;
  language?: ContentLanguage | null;
  scope?: MemeSearchScope | null;
  collectionIds?: string[];
}

export const INFINITE_FEED_OBSERVER_ROOT_MARGIN = '420px 0px';

export interface MemeFeedLoadState {
  hasMore: boolean;
  loading: boolean;
  errorMessage: string | null | undefined;
  itemCount: number;
}

export function canLoadNextMemePage({ hasMore, loading, errorMessage, itemCount }: MemeFeedLoadState): boolean {
  return hasMore && !loading && !errorMessage && itemCount > 0;
}

export function appendUniqueMemeResults(
  existing: PublicMemeSearchResultRead[],
  incoming: PublicMemeSearchResultRead[]
): PublicMemeSearchResultRead[] {
  const seen = new Set(existing.map((item) => item.meme.id));
  const merged = [...existing];

  for (const item of incoming) {
    if (!seen.has(item.meme.id)) {
      merged.push(item);
      seen.add(item.meme.id);
    }
  }

  return merged;
}

export function uniqueMemeResults(items: PublicMemeSearchResultRead[]): PublicMemeSearchResultRead[] {
  return appendUniqueMemeResults([], items);
}

export function nextMemePageOffset(page: PublicMemeSearchPageRead): number {
  const step = page.limit > 0 ? page.limit : page.items.length;
  return page.offset + step;
}

export function memeFeedKey(filters: MemeFeedFilters): string {
  return JSON.stringify({
    query: filters.query.trim(),
    tags: filters.tags ?? [],
    includeNsfw: filters.includeNsfw,
    mediaType: filters.mediaType ?? null,
    language: filters.language ?? null,
    scope: filters.scope ?? 'public',
    collectionIds: filters.collectionIds ?? []
  });
}
