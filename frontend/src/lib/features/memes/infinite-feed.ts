import type { ContentKind, ContentLanguage, PublicMemeSearchPageRead, PublicMemeSearchResultRead } from '$lib/api/types';

export interface MemeFeedFilters {
  query: string;
  tags?: string[];
  includeNsfw?: boolean;
  mediaType?: ContentKind | null;
  language?: ContentLanguage | null;
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
    language: filters.language ?? null
  });
}
