import type { PublicMemeCardRead, PublicMemeDetailRead } from '$lib/api/types';
import { memeDownloadUrl, memeTitle } from '$lib/memeActions';

export type MemeDetailRelatedSource =
  | { kind: 'tag'; tag: string; memes: PublicMemeCardRead[] }
  | { kind: 'trending'; memes: PublicMemeCardRead[] }
  | null;

export interface MemeDetailViewModel {
  title: string;
  description: string | null;
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
  memes: PublicMemeCardRead[];
}

export function buildMemeDetailView(meme: PublicMemeDetailRead): MemeDetailViewModel {
  const fileCount = meme.files.length || (meme.primary_file ? 1 : 0);
  const primaryFileFacts = fileFacts(meme.primary_file);

  return {
    title: memeTitle(meme) === 'Untitled meme' ? 'Meme detail' : memeTitle(meme),
    description: firstText(meme.seo_description, meme.caption, meme.ocr_text),
    bodyText: firstText(meme.seo_body_text),
    detectedText: firstText(meme.ocr_text),
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
  if (source?.kind === 'tag') {
    return {
      heading: `More from #${source.tag}`,
      description: 'Public catalog results from this meme\'s first tag. This is tag discovery, not a similarity ranking.',
      href: `/tags/${encodeURIComponent(source.tag)}`,
      linkLabel: `Open #${source.tag}`,
      memes: withoutCurrentMeme(source.memes, meme.id)
    };
  }

  if (source?.kind === 'trending') {
    return {
      heading: 'Trending public memes',
      description: 'No tag fallback is available for this meme, so this shows public trending memes instead of similar results.',
      href: '/trends',
      linkLabel: 'Open trends',
      memes: withoutCurrentMeme(source.memes, meme.id)
    };
  }

  return {
    heading: 'Discover more memes',
    description: 'Discovery results are unavailable right now. The meme detail still works for sharing and downloading.',
    href: '/trends',
    linkLabel: 'Open trends',
    memes: []
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

function withoutCurrentMeme(memes: PublicMemeCardRead[], memeId: string): PublicMemeCardRead[] {
  return memes.filter((item) => item.id !== memeId).slice(0, 6);
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
