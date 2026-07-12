import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CurrentSessionRead, TelegramLinkStartRead } from '$lib/api/types';
import {
  buildTelegramStartCommand,
  createSingleFlightPollLoop,
  isFullSession,
  LOGIN_PROVIDER_OPTIONS,
  refreshTelegramSession,
  TELEGRAM_SESSION_REFRESH_ERROR,
  telegramExpiryLabel
} from './telegram-login';

describe('telegram-login helpers', () => {
  it('builds the manual bot command using the backend link_ start prefix', () => {
    expect(buildTelegramStartCommand({ code: 'abc_123' } as TelegramLinkStartRead)).toBe('/start link_abc_123');
  });

  it('formats expiry labels in minutes and seconds', () => {
    expect(telegramExpiryLabel({ expires_in_seconds: 600 } as TelegramLinkStartRead)).toBe('Expires in about 10 minutes');
    expect(telegramExpiryLabel({ expires_in_seconds: 45 } as TelegramLinkStartRead)).toBe('Expires in less than 1 minute');
  });

  it('detects full sessions after Telegram redemption or merge repair', () => {
    expect(isFullSession(null)).toBe(false);
    expect(isFullSession({ user: { account_type: 'guest' } } as CurrentSessionRead)).toBe(false);
    expect(isFullSession({ user: { account_type: 'full' } } as CurrentSessionRead)).toBe(true);
  });

  it('keeps Telegram active while Google and email are marked as later providers', () => {
    expect(LOGIN_PROVIDER_OPTIONS).toMatchObject([
      { id: 'telegram', label: 'Telegram', status: 'available' },
      { id: 'google', label: 'Google', status: 'coming_later' },
      { id: 'email', label: 'Email', status: 'coming_later' }
    ]);
  });
});

describe('Telegram login polling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('waits for each request to settle before scheduling the next request', async () => {
    const requests = [deferred<CurrentSessionRead | null>(), deferred<CurrentSessionRead | null>()];
    let requestIndex = 0;
    let activeRequests = 0;
    let maxActiveRequests = 0;
    const request = vi.fn(() => {
      const pending = requests[requestIndex++];
      activeRequests += 1;
      maxActiveRequests = Math.max(maxActiveRequests, activeRequests);
      return pending.promise.finally(() => {
        activeRequests -= 1;
      });
    });
    const onResult = vi.fn();
    const poller = createSingleFlightPollLoop({ intervalMs: 1_000, request, onResult });

    poller.start();
    expect(request).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(5_000);
    expect(request).toHaveBeenCalledTimes(1);
    expect(maxActiveRequests).toBe(1);

    requests[0].resolve(null);
    await vi.advanceTimersByTimeAsync(999);
    expect(onResult).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1);
    expect(request).toHaveBeenCalledTimes(2);
    expect(maxActiveRequests).toBe(1);
    poller.stop();
  });

  it('aborts the in-flight request and leaves no recurring timer when stopped', async () => {
    const requestSignals: AbortSignal[] = [];
    const request = vi.fn((signal: AbortSignal) => {
      requestSignals.push(signal);
      return new Promise<CurrentSessionRead | null>((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('Polling stopped', 'AbortError')), { once: true });
      });
    });
    const onResult = vi.fn();
    const poller = createSingleFlightPollLoop({ intervalMs: 1_000, request, onResult });

    poller.start();
    poller.stop();

    expect(requestSignals).toHaveLength(1);
    expect(requestSignals[0].aborted).toBe(true);
    expect(poller.isRunning()).toBe(false);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(request).toHaveBeenCalledTimes(1);
    expect(onResult).not.toHaveBeenCalled();
  });

  it('suppresses a stale result after a newer polling generation starts', async () => {
    const staleRequest = deferred<CurrentSessionRead | null>();
    const currentRequest = deferred<CurrentSessionRead | null>();
    const request = vi.fn()
      .mockImplementationOnce(() => staleRequest.promise)
      .mockImplementationOnce(() => currentRequest.promise);
    const onResult = vi.fn();
    const poller = createSingleFlightPollLoop({ intervalMs: 1_000, request, onResult });

    poller.start();
    poller.start();
    expect(request).toHaveBeenCalledTimes(2);

    staleRequest.resolve(sessionPayload('full'));
    await vi.advanceTimersByTimeAsync(0);
    expect(onResult).not.toHaveBeenCalled();

    currentRequest.resolve(sessionPayload('guest'));
    await vi.advanceTimersByTimeAsync(0);
    expect(onResult).toHaveBeenCalledOnce();
    expect(onResult).toHaveBeenCalledWith(expect.objectContaining({ user: expect.objectContaining({ account_type: 'guest' }) }));
    poller.stop();
  });

  it('keeps polling after refresh failure and stops only after a deterministic refresh retry succeeds', async () => {
    const session = sessionPayload('full');
    const request = vi.fn().mockResolvedValue(session);
    const invalidateSession = vi.fn()
      .mockRejectedValueOnce(new Error('refresh failed'))
      .mockResolvedValueOnce(undefined);
    const refreshResults: Awaited<ReturnType<typeof refreshTelegramSession>>[] = [];
    const poller = createSingleFlightPollLoop({
      intervalMs: 1_000,
      request,
      onResult: async () => {
        const result = await refreshTelegramSession(invalidateSession);
        refreshResults.push(result);
        return result.shouldContinuePolling;
      }
    });

    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(refreshResults[0]).toEqual({
      completed: false,
      shouldContinuePolling: true,
      errorMessage: TELEGRAM_SESSION_REFRESH_ERROR
    });
    expect(poller.isRunning()).toBe(true);

    await vi.advanceTimersByTimeAsync(1_000);
    expect(invalidateSession).toHaveBeenCalledTimes(2);
    expect(refreshResults[1]).toEqual({ completed: true, shouldContinuePolling: false, errorMessage: null });
    expect(poller.isRunning()).toBe(false);
  });
});

describe('Telegram session refresh completion', () => {
  it('reports completion only after invalidation resolves', async () => {
    const invalidation = deferred<void>();
    let settled = false;
    const refreshPromise = refreshTelegramSession(() => invalidation.promise).then((result) => {
      settled = true;
      return result;
    });

    await Promise.resolve();
    expect(settled).toBe(false);

    invalidation.resolve(undefined);
    await expect(refreshPromise).resolves.toEqual({ completed: true, shouldContinuePolling: false, errorMessage: null });
  });
});

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function sessionPayload(accountType: 'full' | 'guest'): CurrentSessionRead {
  return {
    user: {
      id: 'poll-user',
      account_type: accountType,
      telegram_id: accountType === 'full' ? 1 : null,
      google_id: null,
      email: null,
      email_verified_at: null,
      language: 'any',
      nsfw_enabled: false,
      token_nonce: 0,
      status: 'active',
      guest_expires_at: accountType === 'guest' ? '2099-12-31T23:59:59Z' : null,
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
      telegram_linked: accountType === 'full'
    }
  };
}
