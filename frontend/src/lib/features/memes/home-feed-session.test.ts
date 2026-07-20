import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api/client';

import {
  homeFeedStorageKey,
  loadHomeFeedWithCursorRecovery,
  loadRestorableHomeFeed,
  persistRestorableHomeFeed,
  type RestorableHomeFeedState
} from './home-feed-session';

describe('restorable home feed sessions', () => {
  it('keys state by viewer and filter identity', () => {
    expect(homeFeedStorageKey('viewer-a', 'filters-a')).toBe(homeFeedStorageKey('viewer-a', 'filters-a'));
    expect(homeFeedStorageKey('viewer-a', 'filters-a')).not.toBe(homeFeedStorageKey('viewer-b', 'filters-a'));
    expect(homeFeedStorageKey('viewer-a', 'filters-a')).not.toBe(homeFeedStorageKey('viewer-a', 'filters-b'));
  });

  it('round-trips an unexpired feed pool and rejects another viewer', () => {
    const storage = memoryStorage();
    const state = feedState();
    const key = homeFeedStorageKey(state.viewerId, state.feedKey);

    expect(persistRestorableHomeFeed(storage, key, state, Date.parse('2026-07-20T10:00:00Z'))).toBe(true);
    expect(loadRestorableHomeFeed(storage, key, { viewerId: 'viewer-1', feedKey: 'filters-1' }, Date.parse('2026-07-20T10:30:00Z'))).toEqual(state);
    expect(loadRestorableHomeFeed(storage, key, { viewerId: 'viewer-2', feedKey: 'filters-1' }, Date.parse('2026-07-20T10:30:00Z'))).toBeNull();
    expect(storage.removeItem).toHaveBeenCalledWith(key);
  });

  it('removes expired or malformed pools instead of restoring them', () => {
    const storage = memoryStorage();
    const state = feedState();
    const key = homeFeedStorageKey(state.viewerId, state.feedKey);
    persistRestorableHomeFeed(storage, key, state, Date.parse('2026-07-20T10:00:00Z'));

    expect(loadRestorableHomeFeed(storage, key, state, Date.parse(state.expiresAt))).toBeNull();
    storage.setItem(key, '{not json');
    expect(loadRestorableHomeFeed(storage, key, state)).toBeNull();

    storage.setItem(key, JSON.stringify({ ...state, items: [{}] }));
    expect(loadRestorableHomeFeed(storage, key, state)).toBeNull();
  });

  it('restarts page one exactly once when a signed cursor has expired', async () => {
    const load = vi.fn(async (cursor: string | null) => {
      if (cursor) throw new ApiError(410, 'Expired.', undefined, 'feed_cursor_expired');
      return { feed_session_id: 'fresh-feed' };
    });
    const onExpired = vi.fn();

    await expect(loadHomeFeedWithCursorRecovery({ cursor: 'expired-cursor', load, onExpired })).resolves.toEqual({
      page: { feed_session_id: 'fresh-feed' },
      restarted: true
    });
    expect(load).toHaveBeenNthCalledWith(1, 'expired-cursor');
    expect(load).toHaveBeenNthCalledWith(2, null);
    expect(onExpired).toHaveBeenCalledOnce();
  });
});

function feedState(): RestorableHomeFeedState {
  return {
    feedKey: 'filters-1',
    viewerId: 'viewer-1',
    feedSessionId: 'feed-1',
    expiresAt: '2026-07-20T12:00:00Z',
    items: [],
    total: 200,
    limit: 12,
    offset: 0,
    nextCursor: 'signed-cursor',
    hasMore: true,
    scrollY: 480
  };
}

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => values.set(key, value)),
    removeItem: vi.fn((key: string) => values.delete(key))
  };
}
