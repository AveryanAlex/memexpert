import { describe, expect, it } from 'vitest';

import { passthroughUpstreamResponse } from './proxyResponse';

describe('proxy response passthrough', () => {
  it('copies content type, status metadata, body, and multiple set-cookie headers', async () => {
    const upstream = new Response(JSON.stringify({ ok: true }), {
      status: 202,
      statusText: 'Accepted',
      headers: { 'content-type': 'application/json' }
    });
    const headersWithSetCookie = upstream.headers as Headers & { getSetCookie?: () => string[] };
    headersWithSetCookie.getSetCookie = () => [
      'memexpert_access_token=new; Path=/; HttpOnly',
      'memexpert_refresh_token=next; Path=/; HttpOnly'
    ];

    const response = passthroughUpstreamResponse(upstream);

    expect(response.status).toBe(202);
    expect(response.statusText).toBe('Accepted');
    expect(response.headers.get('content-type')).toBe('application/json');
    expect(response.headers.get('set-cookie')).toContain('memexpert_access_token=new');
    expect(response.headers.get('set-cookie')).toContain('memexpert_refresh_token=next');
    await expect(response.json()).resolves.toEqual({ ok: true });
  });
});
