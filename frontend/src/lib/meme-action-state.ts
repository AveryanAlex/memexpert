import { getContext, setContext } from 'svelte';
import { writable, type Readable } from 'svelte/store';

export interface MemeActionStatePatch {
  favorited?: boolean;
  favoritePending?: boolean;
  saved?: boolean;
  pinned?: boolean;
  pinPending?: boolean;
  likeCount?: number;
  savedCollectionIds?: readonly string[];
}

export type MemeActionStateSnapshot = Readonly<Record<string, MemeActionStatePatch>>;

export type MemeActionViewerReader = () => string | null;
export type CoordinatedMemeAction = 'favorite' | 'pin';

export interface MemeActionOperation {
  readonly memeId: string;
  readonly action: CoordinatedMemeAction;
  readonly operationId: number;
  readonly viewerRevision: number;
}

export interface MemeActionStateStore extends Readable<MemeActionStateSnapshot> {
  syncViewer: (viewerId: string | null) => void;
  publish: (memeId: string, patch: MemeActionStatePatch) => void;
  beginOperation: (memeId: string, action: CoordinatedMemeAction) => MemeActionOperation | null;
  completeOperation: (operation: MemeActionOperation, patch?: MemeActionStatePatch) => boolean;
}

export const memeActionStateContextKey = Symbol('meme-action-state');

const emptySnapshot: MemeActionStateSnapshot = Object.freeze({});

export function createMemeActionState(initialViewerId: string | null = null): MemeActionStateStore {
  const state = writable<MemeActionStateSnapshot>(emptySnapshot);
  const activeOperations = new Map<string, number>();
  let viewerId = initialViewerId;
  let viewerRevision = 0;
  let nextOperationId = 1;

  const publish = (memeId: string, patch: MemeActionStatePatch) => {
    state.update((current) => ({
      ...current,
      [memeId]: {
        ...current[memeId],
        ...patch
      }
    }));
  };

  return {
    subscribe: state.subscribe,
    syncViewer: (nextViewerId) => {
      if (nextViewerId === viewerId) return;

      viewerId = nextViewerId;
      viewerRevision += 1;
      activeOperations.clear();
      state.set(emptySnapshot);
    },
    publish,
    beginOperation: (memeId, action) => {
      const key = operationKey(memeId, action);
      if (activeOperations.has(key)) return null;

      const operation: MemeActionOperation = {
        memeId,
        action,
        operationId: nextOperationId,
        viewerRevision
      };
      nextOperationId += 1;
      activeOperations.set(key, operation.operationId);
      publish(memeId, pendingPatch(action, true));
      return operation;
    },
    completeOperation: (operation, patch = {}) => {
      const key = operationKey(operation.memeId, operation.action);
      if (
        operation.viewerRevision !== viewerRevision ||
        activeOperations.get(key) !== operation.operationId
      ) {
        return false;
      }

      activeOperations.delete(key);
      publish(operation.memeId, {
        ...patch,
        ...pendingPatch(operation.action, false)
      });
      return true;
    }
  };
}

function operationKey(memeId: string, action: CoordinatedMemeAction): string {
  return `${memeId}:${action}`;
}

function pendingPatch(action: CoordinatedMemeAction, pending: boolean): MemeActionStatePatch {
  return action === 'favorite' ? { favoritePending: pending } : { pinPending: pending };
}

export function provideMemeActionState(readInitialViewerId: MemeActionViewerReader): MemeActionStateStore {
  const state = createMemeActionState(readInitialViewerId());
  setContext(memeActionStateContextKey, state);
  return state;
}

export function readMemeActionState(): MemeActionStateStore {
  return getContext<MemeActionStateStore | undefined>(memeActionStateContextKey) ?? createMemeActionState();
}
