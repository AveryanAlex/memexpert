import { ApiError, fetchMemeLibrary, type ApiFetch } from '$lib/api/client';
import type { MemeLibraryRead } from '$lib/api/types';

export interface BackendPageRequest {
  fetch: ApiFetch;
  baseUrl: string;
  cookieHeader?: string;
  onResponse?: (response: Response) => void;
}

export async function loadLibraryPage(request: BackendPageRequest): Promise<{
  library: MemeLibraryRead | null;
  libraryError: string | null;
}> {
  try {
    return { library: await fetchMemeLibrary(request), libraryError: null };
  } catch (error) {
    return {
      library: null,
      libraryError: error instanceof ApiError ? error.message : 'Could not reach the meme library API.'
    };
  }
}
