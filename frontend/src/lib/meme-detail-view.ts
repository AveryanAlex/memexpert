import type { PublicMemeDetailRead } from '$lib/api/types';
import { memeDownloadUrl } from '$lib/memeActions';

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
