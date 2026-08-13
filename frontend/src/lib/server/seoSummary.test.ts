import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';

const baseUrl = 'https://api.memexpert.test';

afterEach(() => {
  vi.useRealTimers();
  vi.resetModules();
});

describe('fetchCachedSeoSummary', () => {
  it('deduplicates concurrent requests and reuses the cached summary', async () => {
    const { fetchCachedSeoSummary } = await import('./seoSummary');
    let resolveSummary: ((response: Response) => void) | undefined;
    const summaryResponse = new Promise<Response>((resolve) => {
      resolveSummary = resolve;
    });
    const mockFetch = vi.fn(() => summaryResponse) satisfies ApiFetch;

    const firstRequest = fetchCachedSeoSummary(mockFetch, baseUrl);
    const secondRequest = fetchCachedSeoSummary(mockFetch, baseUrl);

    expect(mockFetch).toHaveBeenCalledOnce();
    resolveSummary?.(jsonResponse(12_345));
    await expect(Promise.all([firstRequest, secondRequest])).resolves.toEqual([summary(12_345), summary(12_345)]);

    await expect(fetchCachedSeoSummary(mockFetch, baseUrl)).resolves.toEqual(summary(12_345));
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('retries after a failed request instead of caching the failure', async () => {
    const { fetchCachedSeoSummary } = await import('./seoSummary');
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce(new Response('unavailable', { status: 503 }))
      .mockResolvedValueOnce(jsonResponse(99)) satisfies ApiFetch;

    await expect(fetchCachedSeoSummary(mockFetch, baseUrl)).rejects.toThrow('Catalog API returned 503');
    await expect(fetchCachedSeoSummary(mockFetch, baseUrl)).resolves.toEqual(summary(99));
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('refreshes the summary after the cache expires', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-13T07:00:00Z'));
    const { fetchCachedSeoSummary } = await import('./seoSummary');
    const mockFetch = vi.fn().mockResolvedValueOnce(jsonResponse(10)).mockResolvedValueOnce(jsonResponse(11)) satisfies ApiFetch;

    await expect(fetchCachedSeoSummary(mockFetch, baseUrl)).resolves.toEqual(summary(10));
    await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    await expect(fetchCachedSeoSummary(mockFetch, baseUrl)).resolves.toEqual(summary(11));
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });
});

function summary(publicSafeMemeCount: number) {
  return { public_safe_meme_count: publicSafeMemeCount, tag_count: 3, template_count: 2, updated_at: null };
}

function jsonResponse(publicSafeMemeCount: number): Response {
  return new Response(JSON.stringify(summary(publicSafeMemeCount)), { headers: { 'content-type': 'application/json' } });
}
