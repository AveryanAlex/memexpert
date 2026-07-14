import { getContext, setContext } from 'svelte';
import { writable, type Readable } from 'svelte/store';

export interface MemeActionStatePatch {
  favorited?: boolean;
  saved?: boolean;
  pinned?: boolean;
  likeCount?: number;
}

export type MemeActionStateSnapshot = Readonly<Record<string, MemeActionStatePatch>>;

export interface MemeActionStateStore extends Readable<MemeActionStateSnapshot> {
  syncViewer: (viewerId: string | null) => void;
  publish: (memeId: string, patch: MemeActionStatePatch) => void;
}

export type MemeActionViewerReader = () => string | null;

export const memeActionStateContextKey = Symbol('meme-action-state');

const emptySnapshot: MemeActionStateSnapshot = Object.freeze({});

export function createMemeActionState(initialViewerId: string | null = null): MemeActionStateStore {
  const state = writable<MemeActionStateSnapshot>(emptySnapshot);
  let viewerId = initialViewerId;

  return {
    subscribe: state.subscribe,
    syncViewer: (nextViewerId) => {
      if (nextViewerId === viewerId) return;

      viewerId = nextViewerId;
      state.set(emptySnapshot);
    },
    publish: (memeId, patch) => {
      state.update((current) => ({
        ...current,
        [memeId]: {
          ...current[memeId],
          ...patch
        }
      }));
    }
  };
}

export function provideMemeActionState(readInitialViewerId: MemeActionViewerReader): MemeActionStateStore {
  const state = createMemeActionState(readInitialViewerId());
  setContext(memeActionStateContextKey, state);
  return state;
}

export function readMemeActionState(): MemeActionStateStore {
  return getContext<MemeActionStateStore | undefined>(memeActionStateContextKey) ?? createMemeActionState();
}
