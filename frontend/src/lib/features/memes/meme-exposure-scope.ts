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
      state.set({ clientReady, pageKey, visitEpoch });
    },
    beginClientVisit: (nonce) => {
      clientVisitNonce = nonce.trim() || 'client';
      pageSequence = 0;
      visitEpoch = `${clientVisitNonce}:${pageSequence}`;
      clientReady = true;
      recorded = new Set<string>();
      state.set({ clientReady, pageKey, visitEpoch });
    },
    resolveExposureId: (providedId, placementKey) => {
      if (providedId?.trim()) return providedId;
      return `web_${stableToken(`${pageKey}:${visitEpoch}`)}_${stableToken(placementKey)}`;
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

function stableToken(value: string): string {
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    first = Math.imul(first ^ code, 0x01000193);
    second = Math.imul(second ^ code, 0x85ebca6b);
  }
  return `${(first >>> 0).toString(36)}${(second >>> 0).toString(36)}`;
}
