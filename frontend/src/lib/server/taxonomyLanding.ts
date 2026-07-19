import {
  DEFAULT_PAGE_SIZE,
  ApiError,
  fetchTagLanding,
  fetchTemplateLanding,
  type ApiFetch
} from '$lib/api/client';
import type { PublicMemeLandingRead } from '$lib/api/types';

export type TaxonomyLandingKind = 'tag' | 'template';

export interface TaxonomyLandingLoadRequest {
  kind: TaxonomyLandingKind;
  slug: string;
  rawOffset: string | null;
  fetch: ApiFetch;
  baseUrl: string;
  cookieHeader?: string;
}

export interface TaxonomyLandingLoadResult {
  landing: PublicMemeLandingRead | null;
  offset: number;
  errorMessage: string | null;
}

export async function loadTaxonomyLanding({
  kind,
  slug,
  rawOffset,
  fetch,
  baseUrl,
  cookieHeader
}: TaxonomyLandingLoadRequest): Promise<TaxonomyLandingLoadResult> {
  const offset = readOffset(rawOffset);
  const fetchLanding = kind === 'tag' ? fetchTagLanding : fetchTemplateLanding;

  try {
    const landing = await fetchLanding({
      fetch,
      baseUrl,
      slug,
      limit: DEFAULT_PAGE_SIZE,
      offset,
      cookieHeader
    });

    return { landing, offset, errorMessage: null };
  } catch (error) {
    return {
      landing: null,
      offset,
      errorMessage: error instanceof ApiError ? error.message : 'Could not reach the meme catalog API.'
    };
  }
}

function readOffset(raw: string | null): number {
  const offset = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(offset) && offset > 0 ? offset : 0;
}
