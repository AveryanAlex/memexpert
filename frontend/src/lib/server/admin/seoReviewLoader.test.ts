import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { load as loadSeoReviewPage } from '../../../routes/admin/content/seo/+page.server';
import { MAX_SEO_REVIEW_PAGE, SEO_REVIEW_PAGE_SIZE } from './seoPagination';

describe('SEO review page loader', () => {
  it('loads the first page with one extra row to determine the next link', async () => {
    const { result, calls } = await loadPage('/admin/content/seo', rows(SEO_REVIEW_PAGE_SIZE + 1));

    expect(calls).toEqual([{ limit: String(SEO_REVIEW_PAGE_SIZE + 1), offset: '0' }]);
    expect(result.reviews).toHaveLength(SEO_REVIEW_PAGE_SIZE);
    expect(result.paging).toEqual({ page: 1, pageSize: SEO_REVIEW_PAGE_SIZE, hasPrevious: false, hasNext: true });
    expect(result.loadError).toBeNull();
  });

  it('uses the URL page to load a middle page with previous and next links', async () => {
    const { result, calls } = await loadPage('/admin/content/seo?page=2', rows(SEO_REVIEW_PAGE_SIZE + 1));

    expect(calls).toEqual([{ limit: String(SEO_REVIEW_PAGE_SIZE + 1), offset: String(SEO_REVIEW_PAGE_SIZE) }]);
    expect(result.reviews).toHaveLength(SEO_REVIEW_PAGE_SIZE);
    expect(result.paging).toEqual({ page: 2, pageSize: SEO_REVIEW_PAGE_SIZE, hasPrevious: true, hasNext: true });
  });

  it('renders an end page without a next link when no extra row is returned', async () => {
    const { result, calls } = await loadPage('/admin/content/seo?page=3', rows(4));

    expect(calls).toEqual([{ limit: String(SEO_REVIEW_PAGE_SIZE + 1), offset: String(SEO_REVIEW_PAGE_SIZE * 2) }]);
    expect(result.reviews).toHaveLength(4);
    expect(result.paging).toEqual({ page: 3, pageSize: SEO_REVIEW_PAGE_SIZE, hasPrevious: true, hasNext: false });
  });

  it('normalizes invalid pages, keeps normal records reachable, and clamps only beyond safe offset arithmetic', async () => {
    const cases: Array<[string, number]> = [
      ['/admin/content/seo?page=0', 1],
      ['/admin/content/seo?page=-4', 1],
      ['/admin/content/seo?page=two', 1],
      ['/admin/content/seo?page=10001', 10001],
      [`/admin/content/seo?page=${MAX_SEO_REVIEW_PAGE + 1}`, MAX_SEO_REVIEW_PAGE]
    ];
    for (const [path, expectedPage] of cases) {
      const { result, calls } = await loadPage(path, rows(1));
      expect(calls).toEqual([{ limit: String(SEO_REVIEW_PAGE_SIZE + 1), offset: String((expectedPage - 1) * SEO_REVIEW_PAGE_SIZE) }]);
      expect(result.paging.page).toBe(expectedPage);
    }
  });

  it('keeps offset arithmetic safe and suppresses Next at the technical final page', async () => {
    const { result, calls } = await loadPage(
      `/admin/content/seo?page=${MAX_SEO_REVIEW_PAGE}`,
      rows(SEO_REVIEW_PAGE_SIZE + 1)
    );

    expect((MAX_SEO_REVIEW_PAGE - 1) * SEO_REVIEW_PAGE_SIZE).toBeLessThanOrEqual(Number.MAX_SAFE_INTEGER);
    expect(MAX_SEO_REVIEW_PAGE * SEO_REVIEW_PAGE_SIZE).toBeGreaterThan(Number.MAX_SAFE_INTEGER);
    expect(calls).toEqual([
      {
        limit: String(SEO_REVIEW_PAGE_SIZE + 1),
        offset: String((MAX_SEO_REVIEW_PAGE - 1) * SEO_REVIEW_PAGE_SIZE)
      }
    ]);
    expect(result.reviews).toHaveLength(SEO_REVIEW_PAGE_SIZE);
    expect(result.paging).toEqual({
      page: MAX_SEO_REVIEW_PAGE,
      pageSize: SEO_REVIEW_PAGE_SIZE,
      hasPrevious: true,
      hasNext: false
    });
  });
});

async function loadPage(path: string, payload: unknown[]) {
  const calls: Array<{ limit: string | null; offset: string | null }> = [];
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    calls.push({ limit: url.searchParams.get('limit'), offset: url.searchParams.get('offset') });
    return jsonResponse(payload);
  }) satisfies ApiFetch;
  const url = new URL(path, 'http://frontend.test');
  const result = (await loadSeoReviewPage({
    fetch,
    request: new Request(url),
    url
  } as never)) as {
    reviews: unknown[];
    paging: { page: number; pageSize: number; hasPrevious: boolean; hasNext: boolean };
    loadError: string | null;
  };
  return { result, calls };
}

function rows(count: number): unknown[] {
  return Array.from({ length: count }, (_, index) => ({ meme: { id: `meme-${index}` } }));
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { headers: { 'content-type': 'application/json' } });
}
