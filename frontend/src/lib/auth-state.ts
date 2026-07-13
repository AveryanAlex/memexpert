import { getContext, setContext } from 'svelte';
import { writable, type Readable } from 'svelte/store';

import type { CurrentSessionRead, UserRead } from '$lib/api/types';

export interface AuthStateSnapshot {
  session: CurrentSessionRead | null;
  sessionError: string | null;
}

export interface AuthStateStore extends Readable<AuthStateSnapshot> {
  syncFromServer: (snapshot: AuthStateSnapshot) => void;
  setSession: (session: CurrentSessionRead | null) => void;
  updateUser: (user: UserRead) => void;
}

export type AuthStateSnapshotReader = () => AuthStateSnapshot;

export const authStateContextKey = Symbol('auth-state');

const CLIENT_SESSION_RECONCILIATION_WINDOW_MS = 15_000;

const emptyAuthState: AuthStateSnapshot = Object.freeze({
  session: null,
  sessionError: null
});

export function createAuthState(initial: AuthStateSnapshot = emptyAuthState): AuthStateStore {
  const state = writable<AuthStateSnapshot>(initial);
  // A browser mutation can finish while an older layout request is still in flight.
  let pendingClientSession: CurrentSessionRead | null = null;
  let pendingClientSessionExpiresAt = 0;

  return {
    subscribe: state.subscribe,
    syncFromServer: (snapshot) => {
      if (
        pendingClientSession &&
        Date.now() < pendingClientSessionExpiresAt &&
        !serverSnapshotIncludes(snapshot.session, pendingClientSession)
      ) {
        state.set({ session: pendingClientSession, sessionError: null });
        return;
      }

      pendingClientSession = null;
      pendingClientSessionExpiresAt = 0;
      state.set(snapshot);
    },
    setSession: (session) => {
      pendingClientSession = session;
      pendingClientSessionExpiresAt = session ? Date.now() + CLIENT_SESSION_RECONCILIATION_WINDOW_MS : 0;
      state.set({ session, sessionError: null });
    },
    updateUser: (user) => {
      state.update((current) => {
        if (!current.session) return current;

        pendingClientSession = { ...current.session, user };
        pendingClientSessionExpiresAt = Date.now() + CLIENT_SESSION_RECONCILIATION_WINDOW_MS;
        return {
          session: pendingClientSession,
          sessionError: null
        };
      });
    }
  };
}

export function provideAuthState(readInitial: AuthStateSnapshotReader): AuthStateStore {
  const state = createAuthState(readInitial());
  setContext(authStateContextKey, state);
  return state;
}

export function readAuthState(readFallback: AuthStateSnapshotReader = () => emptyAuthState): AuthStateStore {
  return getContext<AuthStateStore | undefined>(authStateContextKey) ?? createAuthState(readFallback());
}

function serverSnapshotIncludes(serverSession: CurrentSessionRead | null, clientSession: CurrentSessionRead): boolean {
  if (
    !serverSession ||
    serverSession.user.id !== clientSession.user.id ||
    serverSession.user.account_type !== clientSession.user.account_type
  ) {
    return false;
  }

  if (serverSession.user.updated_at === clientSession.user.updated_at) {
    return JSON.stringify(serverSession) === JSON.stringify(clientSession);
  }

  return serverSession.user.updated_at > clientSession.user.updated_at;
}
