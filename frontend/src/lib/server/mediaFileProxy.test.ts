import { describe, expect, it, vi } from 'vitest';
import { proxyMediaFile } from './mediaFileProxy';
import type { ProxyFetch } from './proxyResponse';

describe('media file proxy', () => {
  it('forwards cookies, encoded params, and manual redirect handling', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe(
        'https://api.memexpert.test/api/v1/media/files/file%2Fwith%20space/web-video.mp4?v=generation-token'
      );
      expect(init?.method).toBe('GET');
      expect(init?.redirect).toBe('manual');
      expect(new Headers(init?.headers).get('cookie')).toBe('memexpert_access_token=admin-token');
      return new Response(null, {
        status: 307,
        headers: {
          location: 'https://storage.example.test/signed-object',
          'x-internal-storage-key': 'must-not-leak',
          'set-cookie': 'must-not-leak=1'
        }
      });
    }) satisfies ProxyFetch;

    const response = await proxyMediaFile({
      fetch,
      request: new Request(
        'https://web.memexpert.test/api/v1/media/files/file%2Fwith%20space/web-video.mp4?v=generation-token',
        {
          headers: { cookie: 'memexpert_access_token=admin-token' }
        }
      ),
      apiBaseUrl: 'https://api.memexpert.test',
      fileId: 'file/with space',
      variant: 'web-video.mp4'
    });

    expect(response.status).toBe(307);
    expect(response.headers.get('location')).toBe('https://storage.example.test/signed-object');
    expect(response.headers.get('x-internal-storage-key')).toBeNull();
    expect(response.headers.get('set-cookie')).toBeNull();
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    expect(response.headers.get('pragma')).toBe('no-cache');
    expect(fetch).toHaveBeenCalledOnce();
  });

  it('safely forwards a backend 404 body and content type only', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toBe('https://api.memexpert.test/api/v1/media/files/file-123/preview');
      return new Response(JSON.stringify({ detail: 'Media file was not found.' }), {
        status: 404,
        headers: {
          'content-type': 'application/json',
          'x-backend-debug': 'secret'
        }
      });
    }) satisfies ProxyFetch;

    const response = await proxyMediaFile({
      fetch,
      request: new Request('https://web.memexpert.test/api/v1/media/files/file-123/preview'),
      apiBaseUrl: 'https://api.memexpert.test',
      fileId: 'file-123',
      variant: 'preview'
    });

    expect(response.status).toBe(404);
    expect(response.headers.get('content-type')).toContain('application/json');
    expect(response.headers.get('x-backend-debug')).toBeNull();
    await expect(response.json()).resolves.toEqual({ detail: 'Media file was not found.' });
  });

  it('rejects unsupported variants without contacting the backend', async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 500 })) satisfies ProxyFetch;

    const response = await proxyMediaFile({
      fetch,
      request: new Request('https://web.memexpert.test/api/v1/media/files/file-123/secret-key'),
      apiBaseUrl: 'https://api.memexpert.test',
      fileId: 'file-123',
      variant: 'secret-key'
    });

    expect(response.status).toBe(404);
    await expect(response.json()).resolves.toEqual({ detail: 'Media variant was not found.' });
    expect(fetch).not.toHaveBeenCalled();
  });
});
