import { describe, expect, it, vi } from 'vitest';

import { ApiError, type ApiFetch } from '$lib/api/client';
import type { AdminTelegramChannelFromReferencePayload } from '$lib/api/types';
import { addTelegramSourceWithRetry, isRetryableSourceAddError } from './add-source-client';

describe('resilient Telegram source creation', () => {
  it('retries gateway and network interruptions before returning the idempotent API result', async () => {
    let requestCount = 0;
    const fetch = vi.fn(async () => {
      requestCount += 1;
      if (requestCount === 1) return jsonResponse({ detail: 'Gateway timeout.' }, 504);
      if (requestCount === 2) throw new TypeError('connection reset');
      return jsonResponse({ id: 'source-id', platform_id: 'memach' }, 201);
    }) satisfies ApiFetch;
    const wait = vi.fn(async () => undefined);
    const onRetry = vi.fn();

    const result = await addTelegramSourceWithRetry({
      fetch,
      baseUrl: 'https://beta.memexpert.net',
      body: sourcePayload(),
      retryDelaysMs: [10, 20],
      wait,
      onRetry
    });

    expect(result).toMatchObject({ id: 'source-id', platform_id: 'memach' });
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(wait.mock.calls).toEqual([[10], [20]]);
    expect(onRetry.mock.calls).toEqual([[1, 10], [2, 20]]);
  });

  it('does not retry business-rule failures', async () => {
    const fetch = vi.fn(async () => jsonResponse({ detail: 'Selected account is not ready.' }, 409)) satisfies ApiFetch;
    const wait = vi.fn(async () => undefined);

    await expect(
      addTelegramSourceWithRetry({
        fetch,
        baseUrl: 'https://beta.memexpert.net',
        body: sourcePayload(),
        retryDelaysMs: [10],
        wait
      })
    ).rejects.toEqual(new ApiError(409, 'Selected account is not ready.'));
    expect(fetch).toHaveBeenCalledOnce();
    expect(wait).not.toHaveBeenCalled();
  });

  it('classifies only transient transport and gateway failures as retryable', () => {
    expect(isRetryableSourceAddError(new TypeError('network unavailable'))).toBe(true);
    expect(isRetryableSourceAddError(new ApiError(502, 'Bad gateway'))).toBe(true);
    expect(isRetryableSourceAddError(new ApiError(504, 'Gateway timeout'))).toBe(true);
    expect(isRetryableSourceAddError(new ApiError(409, 'Conflict'))).toBe(false);
    expect(isRetryableSourceAddError(new Error('Validation failed'))).toBe(false);
  });
});

function sourcePayload(): AdminTelegramChannelFromReferencePayload {
  return {
    reference: '@memach',
    telegram_session_id: '11111111-1111-4111-8111-111111111111',
    suggestion_id: null,
    catchup_message_limit: 5_000
  };
}

function jsonResponse(payload: unknown, status: number): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
