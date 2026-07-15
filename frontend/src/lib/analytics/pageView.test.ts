import { describe, expect, it, vi } from 'vitest';

import { consumerPageViewSurface, recordPageView, type PageViewFetch } from './pageView';

describe('consumerPageViewSurface', () => {
  it('reduces supported consumer paths to fixed categories', () => {
    expect(consumerPageViewSurface('/')).toBe('web_home');
    expect(consumerPageViewSurface('/search')).toBe('web_search');
    expect(consumerPageViewSurface('/memes/distracted-boyfriend')).toBe('web_meme_detail');
    expect(consumerPageViewSurface('/trends/compare')).toBe('web_trends');
    expect(consumerPageViewSurface('/collection/11111111-1111-4111-8111-111111111111')).toBe('web_collection');
  });

  it('excludes admin, API, auth, and unknown routes', () => {
    expect(consumerPageViewSurface('/admin/analytics')).toBeNull();
    expect(consumerPageViewSurface('/api/v1/memes/search')).toBeNull();
    expect(consumerPageViewSurface('/auth/callback')).toBeNull();
    expect(consumerPageViewSurface('/unrecognized')).toBeNull();
  });
});

describe('recordPageView', () => {
  it('sends only the coarse category through the same-origin proxy', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 202 }));
    const request = fetchMock as PageViewFetch;

    await recordPageView('web_search', request);

    expect(request).toHaveBeenCalledWith(
      '/api/v1/analytics/page-views',
      expect.objectContaining({
        method: 'POST',
        credentials: 'same-origin',
        keepalive: true,
        body: JSON.stringify({ surface: 'web_search' })
      })
    );
    const call = fetchMock.mock.calls.at(0) as unknown as [RequestInfo | URL, RequestInit];
    const headers = new Headers(call[1].headers);
    expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
  });
});
