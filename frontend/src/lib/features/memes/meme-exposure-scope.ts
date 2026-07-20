import { uuidV7 } from '$lib/analytics/uuid-v7';
import { getContext, setContext } from 'svelte';
import { writable, type Readable } from 'svelte/store';

interface MemeExposureScopeState {
  clientReady: boolean;
  pageKey: string;
  visitEpoch: string;
}

export interface MemeExposureScope extends Readable<MemeExposureScopeState> {
  syncPage: (pageKey: string) => void;
  beginClientVisit: (nonce: string) => void;
  resolveExposureId: (providedId: string | null | undefined, placementKey: string) => string;
  hasRecorded: (exposureId: string) => boolean;
  claim: (exposureId: string) => boolean;
}

export const memeExposureScopeContextKey = Symbol('meme-exposure-scope');

interface MemeExposureIntersection {
  isIntersecting: boolean;
  intersectionRatio: number;
}

export function hasQualifyingMemeExposure(
  entries: readonly MemeExposureIntersection[],
  minimumRatio = 0.25
): boolean {
  return entries.some((entry) => entry.isIntersecting && entry.intersectionRatio >= minimumRatio);
}

export function createMemeExposureScope(initialPageKey = ''): MemeExposureScope {
  let pageKey = initialPageKey;
  let clientVisitNonce = 'ssr';
  let pageSequence = 0;
  let recorded = new Set<string>();
  let generatedExposureIds = new Map<string, string>();
  let visitEpoch = `${clientVisitNonce}:${pageSequence}`;
  let clientReady = false;
  const state = writable<MemeExposureScopeState>({ clientReady, pageKey, visitEpoch });

  return {
    subscribe: state.subscribe,
    syncPage: (nextPageKey) => {
      if (nextPageKey === pageKey) return;
      pageKey = nextPageKey;
      pageSequence += 1;
      visitEpoch = `${clientVisitNonce}:${pageSequence}`;
      recorded = new Set<string>();
      generatedExposureIds = new Map<string, string>();
      state.set({ clientReady, pageKey, visitEpoch });
    },
    beginClientVisit: (nonce) => {
      clientVisitNonce = nonce.trim() || 'client';
      pageSequence = 0;
      visitEpoch = `${clientVisitNonce}:${pageSequence}`;
      clientReady = true;
      recorded = new Set<string>();
      generatedExposureIds = new Map<string, string>();
      state.set({ clientReady, pageKey, visitEpoch });
    },
    resolveExposureId: (providedId, placementKey) => {
      if (providedId?.trim()) return providedId;
      const placementScope = `${pageKey}:${visitEpoch}:${placementKey}`;
      const existingExposureId = generatedExposureIds.get(placementScope);
      if (existingExposureId) return existingExposureId;
      const exposureId = uuidV7();
      generatedExposureIds.set(placementScope, exposureId);
      return exposureId;
    },
    hasRecorded: (exposureId) => recorded.has(exposureId),
    claim: (exposureId) => {
      if (recorded.has(exposureId)) return false;
      recorded.add(exposureId);
      return true;
    }
  };
}

export function provideMemeExposureScope(initialPageKey: string): MemeExposureScope {
  const scope = createMemeExposureScope(initialPageKey);
  setContext(memeExposureScopeContextKey, scope);
  return scope;
}

export function readMemeExposureScope(): MemeExposureScope {
  return getContext<MemeExposureScope | undefined>(memeExposureScopeContextKey) ?? createMemeExposureScope();
}
