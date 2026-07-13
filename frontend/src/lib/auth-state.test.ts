import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, UserRead } from '$lib/api/types';
import { createAuthState } from '$lib/auth-state';

describe('auth state', () => {
  it('publishes an authenticated session immediately and clears stale load errors', () => {
    const state = createAuthState({ session: guestSession(), sessionError: 'Session API unavailable.' });

    state.setSession(fullSession());

    expect(get(state)).toEqual({ session: fullSession(), sessionError: null });
  });

  it('does not let stale layout data overwrite a browser-confirmed auth transition', () => {
    const guest = guestSession();
    const full = fullSession();
    const state = createAuthState({ session: guest, sessionError: null });

    state.setSession(full);
    state.syncFromServer({ session: guest, sessionError: null });

    expect(get(state).session).toEqual(full);

    state.syncFromServer({ session: full, sessionError: null });
    state.syncFromServer({ session: guest, sessionError: null });

    expect(get(state).session).toEqual(guest);
  });

  it('updates session-backed preferences without discarding linked provider state', () => {
    const session = fullSession();
    const state = createAuthState({ session, sessionError: null });
    const user = { ...session.user, language: 'ru', nsfw_enabled: true } satisfies UserRead;

    state.updateUser(user);

    expect(get(state)).toEqual({
      session: { user, linked_providers: session.linked_providers },
      sessionError: null
    });
  });

  it('keeps each store instance independent', () => {
    const first = createAuthState({ session: guestSession(), sessionError: null });
    const second = createAuthState({ session: guestSession(), sessionError: null });

    first.setSession(fullSession());

    expect(get(first).session?.user.account_type).toBe('full');
    expect(get(second).session?.user.account_type).toBe('guest');
  });
});

function guestSession(): CurrentSessionRead {
  return sessionPayload('guest');
}

function fullSession(): CurrentSessionRead {
  return sessionPayload('full');
}

function sessionPayload(accountType: 'guest' | 'full'): CurrentSessionRead {
  const isFull = accountType === 'full';
  return {
    user: {
      id: isFull ? 'full-user' : 'guest-user',
      account_type: accountType,
      telegram_id: isFull ? 123 : null,
      google_id: null,
      email: null,
      email_verified_at: null,
      language: 'any',
      nsfw_enabled: false,
      token_nonce: 0,
      status: 'active',
      guest_expires_at: isFull ? null : '2099-12-31T23:59:59Z',
      active_save_collection_id: null,
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: isFull
    }
  };
}
