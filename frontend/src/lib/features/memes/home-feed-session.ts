import type { PublicMemeSearchResultRead } from '$lib/api/types';
import type { NavigationType } from '@sveltejs/kit';
import { ApiError } from '$lib/api/client';

const HOME_FEED_STORAGE_PREFIX = 'memexpert:home-feed:v2';

export interface RestorableHomeFeedState {
  feedKey: string;
  viewerId: string | null;
  feedSessionId: string;
  expiresAt: string;
  items: PublicMemeSearchResultRead[];
  total: number;
  limit: number;
  offset: number;
  nextCursor: string | null;
  hasMore: boolean;
  scrollY: number;
}

interface CursorRecoveryRequest<T> {
  cursor: string | null;
  load: (cursor: string | null) => Promise<T>;
  onExpired: () => void;
}

export interface CursorRecoveryResult<T> {
  page: T;
  restarted: boolean;
}

export async function loadHomeFeedWithCursorRecovery<T>({
  cursor,
  load,
  onExpired
}: CursorRecoveryRequest<T>): Promise<CursorRecoveryResult<T>> {
  try {
    return { page: await load(cursor), restarted: false };
  } catch (error) {
    if (!cursor || !isExpiredFeedCursorError(error)) throw error;
    onExpired();
    return { page: await load(null), restarted: true };
  }
}

export function isExpiredFeedCursorError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 410 && error.code === 'feed_cursor_expired';
}

export function shouldRestoreHomeFeed(navigationType: NavigationType | null): boolean {
  return navigationType === 'popstate';
}

export function homeFeedStorageKey(viewerId: string | null, feedKey: string): string {
  return `${HOME_FEED_STORAGE_PREFIX}:${stableToken(viewerId ?? 'anonymous')}:${stableToken(feedKey)}`;
}

export function loadRestorableHomeFeed(
  storage: Pick<Storage, 'getItem' | 'removeItem'>,
  key: string,
  expected: Pick<RestorableHomeFeedState, 'feedKey' | 'viewerId'>,
  now = Date.now()
): RestorableHomeFeedState | null {
  let raw: string | null;
  try {
    raw = storage.getItem(key);
  } catch {
    return null;
  }
  if (!raw) return null;

  try {
    const value = JSON.parse(raw) as unknown;
    if (!isRestorableHomeFeedState(value) || value.feedKey !== expected.feedKey || value.viewerId !== expected.viewerId) {
      clearRestorableHomeFeed(storage, key);
      return null;
    }
    const expiresAt = Date.parse(value.expiresAt);
    if (!Number.isFinite(expiresAt) || expiresAt <= now) {
      clearRestorableHomeFeed(storage, key);
      return null;
    }
    return value;
  } catch {
    clearRestorableHomeFeed(storage, key);
    return null;
  }
}

export function persistRestorableHomeFeed(
  storage: Pick<Storage, 'setItem' | 'removeItem'>,
  key: string,
  state: RestorableHomeFeedState,
  now = Date.now()
): boolean {
  const expiresAt = Date.parse(state.expiresAt);
  if (!Number.isFinite(expiresAt) || expiresAt <= now) {
    clearRestorableHomeFeed(storage, key);
    return false;
  }

  try {
    storage.setItem(key, JSON.stringify(state));
    return true;
  } catch {
    return false;
  }
}

export function clearRestorableHomeFeed(storage: Pick<Storage, 'removeItem'>, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // Storage can be unavailable in privacy-restricted embedded browsers.
  }
}

function isRestorableHomeFeedState(value: unknown): value is RestorableHomeFeedState {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const state = value as Partial<RestorableHomeFeedState>;
  return (
    typeof state.feedKey === 'string' &&
    (state.viewerId === null || typeof state.viewerId === 'string') &&
    typeof state.feedSessionId === 'string' &&
    state.feedSessionId.length > 0 &&
    typeof state.expiresAt === 'string' &&
    Array.isArray(state.items) &&
    state.items.length <= 200 &&
    state.items.every(isRestorableHomeFeedItem) &&
    typeof state.total === 'number' &&
    Number.isFinite(state.total) &&
    state.total >= 0 &&
    typeof state.limit === 'number' &&
    Number.isInteger(state.limit) &&
    state.limit > 0 &&
    state.limit <= 100 &&
    typeof state.offset === 'number' &&
    Number.isInteger(state.offset) &&
    state.offset >= 0 &&
    (state.nextCursor === null || typeof state.nextCursor === 'string') &&
    typeof state.hasMore === 'boolean' &&
    typeof state.scrollY === 'number' &&
    Number.isFinite(state.scrollY) &&
    state.scrollY >= 0
  );
}

function isRestorableHomeFeedItem(value: unknown): value is PublicMemeSearchResultRead {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const item = value as Partial<PublicMemeSearchResultRead>;
  if (!item.meme || typeof item.meme !== 'object' || !item.attribution || typeof item.attribution !== 'object') {
    return false;
  }
  return typeof item.meme.id === 'string' && typeof item.attribution.attribution_token === 'string';
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
