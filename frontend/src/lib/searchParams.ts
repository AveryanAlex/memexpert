import type { ContentKind, ContentLanguage } from '$lib/api/types';

export interface SearchRouteState {
  query: string;
  tags: string[];
  includeNsfw: boolean;
  mediaType: ContentKind | null;
  language: ContentLanguage | null;
  offset: number;
}

export const MEDIA_TYPE_OPTIONS: Array<{ value: ContentKind; label: string }> = [
  { value: 'image', label: 'Images' },
  { value: 'gif', label: 'GIFs' },
  { value: 'video', label: 'Videos' },
  { value: 'text', label: 'Text' },
  { value: 'audio', label: 'Audio' },
  { value: 'link', label: 'Links' }
];

export const LANGUAGE_OPTIONS: Array<{ value: ContentLanguage; label: string }> = [
  { value: 'en', label: 'English' },
  { value: 'ru', label: 'Russian' },
  { value: 'mixed', label: 'Mixed' },
  { value: 'none', label: 'No text' }
];

export const QUICK_SEARCH_TAGS = ['reaction', 'cat', 'wholesome', 'anime', 'gaming', 'politics', 'work', 'sports'];

export function parseSearchParams(params: URLSearchParams): SearchRouteState {
  return {
    query: (params.get('q') ?? '').trim(),
    tags: normalizeTags([...params.getAll('tags'), ...params.getAll('category'), ...params.getAll('categories')]),
    includeNsfw: readBoolean(params.get('include_nsfw')),
    mediaType: readMediaType(params.get('media_type')),
    language: readLanguage(params.get('language')),
    offset: readOffset(params.get('offset'))
  };
}

export function buildSearchHref(state: SearchRouteState, changes: Partial<SearchRouteState> = {}): string {
  const next: SearchRouteState = { ...state, ...changes };
  const params = new URLSearchParams();
  const query = next.query.trim();

  if (query) {
    params.set('q', query);
  }

  for (const tag of normalizeTags(next.tags)) {
    params.append('tags', tag);
  }

  params.set('include_nsfw', String(next.includeNsfw));

  if (next.mediaType) {
    params.set('media_type', next.mediaType);
  }

  if (next.language) {
    params.set('language', next.language);
  }

  if (next.offset > 0) {
    params.set('offset', String(next.offset));
  }

  const serialized = params.toString();
  return serialized ? `/search?${serialized}` : '/search';
}

export function normalizeTags(rawTags: Iterable<string>): string[] {
  const tags: string[] = [];
  const seen = new Set<string>();

  for (const raw of rawTags) {
    for (const part of raw.split(',')) {
      const tag = part.trim().replace(/^#+/, '').toLowerCase();
      if (tag && !seen.has(tag)) {
        tags.push(tag);
        seen.add(tag);
      }
    }
  }

  return tags;
}

function readBoolean(raw: string | null): boolean {
  return raw === 'true' || raw === '1' || raw === 'on';
}

function readOffset(raw: string | null): number {
  const offset = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(offset) && offset > 0 ? offset : 0;
}

function readMediaType(raw: string | null): ContentKind | null {
  return MEDIA_TYPE_OPTIONS.some((option) => option.value === raw) ? (raw as ContentKind) : null;
}

function readLanguage(raw: string | null): ContentLanguage | null {
  return LANGUAGE_OPTIONS.some((option) => option.value === raw) ? (raw as ContentLanguage) : null;
}
