import type {
  MemeResultAttributionRead,
  PublicMemeCardRead,
  PublicMemeDetailRead,
  PublicMemeSearchPageRead,
  PublicMemeSearchResultRead
} from '$lib/api/types';
import { memeDownloadUrl } from '$lib/memeActions';

export type MemeDetailRelatedSource =
  | { kind: 'similar'; page: PublicMemeSearchPageRead }
  | { kind: 'tag'; tag: string; items: PublicMemeSearchResultRead[] }
  | { kind: 'trending'; items: PublicMemeSearchResultRead[] }
  | null;

export interface MemeDetailRelatedItem {
  meme: PublicMemeCardRead;
  attribution: MemeResultAttributionRead | null;
}

export interface MemeDetailViewModel {
  title: string;
  metaDescription: string | null;
  leadDescription: string | null;
  bodyText: string | null;
  detectedText: string | null;
  fileCount: number;
  scoreLabel: string;
  mediaFacts: string[];
  primaryFileFacts: string[];
  downloadUrl: string | null;
}

export interface MemeDetailRelatedDiscovery {
  heading: string;
  description: string;
  href: string;
  linkLabel: string;
  items: MemeDetailRelatedItem[];
  memes: PublicMemeCardRead[];
  attributions: Record<string, MemeResultAttributionRead>;
}

export function buildMemeDetailView(meme: PublicMemeDetailRead): MemeDetailViewModel {
  const fileCount = meme.files.length || (meme.primary_file ? 1 : 0);
  const primaryFileFacts = fileFacts(meme.primary_file);
  const title = firstText(meme.seo_title, meme.caption, meme.tags[0]) ?? 'Meme detail';
  const displayed = new Set<string>();
  rememberText(displayed, title);
  const metaDescription = firstText(meme.seo_description, meme.caption, meme.ocr_text);
  const leadDescription = firstDistinctText(displayed, meme.seo_description, meme.caption);
  const bodyText = firstDistinctText(displayed, meme.seo_body_text);
  const detectedText = firstDistinctText(displayed, meme.ocr_text);

  return {
    title,
    metaDescription,
    leadDescription,
    bodyText,
    detectedText,
    fileCount,
    scoreLabel: meme.popularity_score.toFixed(1),
    mediaFacts: [meme.media_type, meme.language, `${meme.like_count} likes`, `${fileCount} ${fileCount === 1 ? 'file' : 'files'}`],
    primaryFileFacts,
    downloadUrl: memeDownloadUrl(meme)
  };
}

export function buildRelatedDiscovery(
  meme: Pick<PublicMemeDetailRead, 'id'>,
  source: MemeDetailRelatedSource
): MemeDetailRelatedDiscovery {
  if (source?.kind === 'similar') {
    const items = withoutCurrentMeme(source.page.items, meme.id);
    return {
      heading: relatedHeading(items),
      description: relatedDescription(items),
      href: '/trends',
      linkLabel: 'Browse trends',
      items,
      memes: items.map((item) => item.meme),
      attributions: attributionMap(items)
    };
  }

  if (source?.kind === 'tag') {
    const items = withoutCurrentMeme(source.items, meme.id);
    return {
      heading: `More from #${source.tag}`,
      description: 'The similar-memes endpoint was unavailable, so this uses public tag discovery as a safe fallback.',
      href: `/tags/${encodeURIComponent(source.tag)}`,
      linkLabel: `Open #${source.tag}`,
      items,
      memes: items.map((item) => item.meme),
      attributions: attributionMap(items)
    };
  }

  if (source?.kind === 'trending') {
    const items = withoutCurrentMeme(source.items, meme.id);
    return {
      heading: 'Trending public memes',
      description: 'The similar-memes endpoint and tag fallback were unavailable, so this shows public trending memes.',
      href: '/trends',
      linkLabel: 'Open trends',
      items,
      memes: items.map((item) => item.meme),
      attributions: attributionMap(items)
    };
  }

  return {
    heading: 'Discover more memes',
    description: 'Discovery results are unavailable right now. The meme detail still works for sharing and downloading.',
    href: '/trends',
    linkLabel: 'Open trends',
    items: [],
    memes: [],
    attributions: {}
  };
}

export function formatFileSize(bytes: number | null | undefined): string | null {
  if (!Number.isFinite(bytes) || !bytes || bytes <= 0) return null;

  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  return `${value >= 10 || unitIndex === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`;
}

function withoutCurrentMeme(items: PublicMemeSearchResultRead[], memeId: string): MemeDetailRelatedItem[] {
  return items.filter((item) => item.meme.id !== memeId).slice(0, 6);
}

function attributionMap(items: MemeDetailRelatedItem[]): Record<string, MemeResultAttributionRead> {
  return Object.fromEntries(items.flatMap((item) => (item.attribution ? [[item.meme.id, item.attribution]] : [])));
}

function relatedHeading(items: MemeDetailRelatedItem[]): string {
  const algorithms = new Set(items.map((item) => item.attribution?.source_algorithm));
  if (algorithms.has('qdrant_similarity')) return 'Similar memes';
  if (algorithms.has('fallback_tag')) return 'Related public memes';
  if (algorithms.has('fallback_template')) return 'More from this template';
  if (algorithms.has('fallback_popular')) return 'Popular public memes';
  return 'Discover more memes';
}

function relatedDescription(items: MemeDetailRelatedItem[]): string {
  const algorithms = new Set(items.map((item) => item.attribution?.source_algorithm));
  const reason = items.find((item) => item.attribution?.reason)?.attribution?.reason;
  if (algorithms.has('qdrant_similarity')) {
    return 'Ranked from this meme\'s source image embedding, with safe public fallbacks appended when needed.';
  }
  if (reason === 'missing_embedding') {
    return 'No source image embedding is available yet, so these are safe public fallback results.';
  }
  if (reason === 'qdrant_failure') {
    return 'Similarity search is temporarily unavailable, so these are safe public fallback results.';
  }
  if (reason === 'similarity_empty') {
    return 'Similarity search returned no accessible matches, so these are safe public fallback results.';
  }
  return 'Safe public fallback results from tags, templates, and popular memes.';
}

function fileFacts(file: PublicMemeDetailRead['primary_file']): string[] {
  if (!file) return [];

  return [file.mime_type, dimensions(file.width, file.height), formatFileSize(file.file_size_bytes)].filter((item): item is string => Boolean(item));
}

function dimensions(width: number | null | undefined, height: number | null | undefined): string | null {
  return width && height ? `${width}x${height}` : null;
}

function firstText(...values: Array<string | null | undefined>): string | null {
  for (const value of values) {
    const trimmed = value?.trim();
    if (trimmed) return trimmed;
  }
  return null;
}

function firstDistinctText(
  displayed: Set<string>,
  ...values: Array<string | null | undefined>
): string | null {
  for (const value of values) {
    const trimmed = value?.trim();
    if (!trimmed) continue;
    const normalized = normalizeMemeDisplayText(trimmed);
    if (!normalized || displayed.has(normalized)) continue;
    displayed.add(normalized);
    return trimmed;
  }
  return null;
}

function rememberText(displayed: Set<string>, value: string | null | undefined): void {
  const normalized = normalizeMemeDisplayText(value);
  if (normalized) displayed.add(normalized);
}

/**
 * Best-effort deterministic fold for visible-text deduplication. ECMAScript's
 * `toLowerCase()` uses Unicode default casing and does not depend on the host
 * locale; it is intentionally not presented as full Unicode case folding.
 */
export function normalizeMemeDisplayText(value: string | null | undefined): string {
  return value?.normalize('NFKC').trim().replace(/\s+/gu, ' ').toLowerCase() ?? '';
}
