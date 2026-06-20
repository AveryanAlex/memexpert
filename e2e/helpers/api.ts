import { expect, type APIRequestContext, type APIResponse, type Page } from '@playwright/test';
import type { SeededE2EUser, SeededMeme } from './seed';

const ACCESS_COOKIE_NAME = 'memexpert_access_token';

export interface SearchItem {
  meme: { id: string; seo_page_slug: string | null; is_nsfw: boolean };
}

export interface SearchPayload {
  items: SearchItem[];
  total: number;
}

export interface HomeFeedItem {
  meme: {
    id: string;
    seo_page_slug: string | null;
    is_nsfw: boolean;
    primary_file: Record<string, unknown> | null;
  };
  attribution: {
    surface: string | null;
    source_algorithm: string | null;
    reason: string | null;
    query: string | null;
    filters: {
      include_nsfw: boolean;
      scope: string | null;
    };
    collection_scope: string | null;
  };
}

export interface HomeFeedPayload {
  items: HomeFeedItem[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  request_id: string;
}

export interface HomeFeedResult {
  response: APIResponse;
  payload: HomeFeedPayload;
}

export class E2EApi {
  constructor(
    private request: APIRequestContext,
    private apiBaseUrl: string,
    private operatorToken: string
  ) {}

  async expectHealthy() {
    const response = await this.request.get(`${this.apiBaseUrl}/health`);
    expect(response.ok()).toBeTruthy();
    await expect(response).toBeOK();
  }

  async searchMemes(params: Record<string, string>): Promise<SearchPayload> {
    const response = await this.request.get(`${this.apiBaseUrl}/api/v1/memes/search`, { params });
    await expect(response).toBeOK();
    return (await response.json()) as SearchPayload;
  }

  async homeFeed(params: Record<string, string>): Promise<HomeFeedResult> {
    const response = await this.request.get(`${this.apiBaseUrl}/api/v1/memes/home-feed`, { params });
    await expect(response).toBeOK();
    return { response, payload: (await response.json()) as HomeFeedPayload };
  }

  expectHomeFeedFallback(payload: HomeFeedPayload, seededMemes: SeededMeme[]) {
    const publicSeededById = new Map(
      seededMemes.filter((meme) => !meme.is_nsfw).map((meme) => [meme.meme_id, meme])
    );
    expect(payload.items.length, 'home feed should return fallback items').toBeGreaterThan(0);

    const seededPublicItems = payload.items.filter((item) => publicSeededById.has(item.meme.id));
    expect(seededPublicItems.length, 'home feed should include seeded public fixtures').toBeGreaterThan(0);

    for (const item of seededPublicItems) {
      const seeded = publicSeededById.get(item.meme.id);
      expect(item.meme).toEqual(
        expect.objectContaining({
          id: seeded?.meme_id,
          seo_page_slug: seeded?.slug,
          is_nsfw: false
        })
      );
    }

    for (const item of payload.items) {
      expect(item.attribution).toEqual(
        expect.objectContaining({
          surface: 'public_api_home_feed',
          source_algorithm: 'fallback_trending',
          reason: 'cold_start_no_positive_signals',
          query: null,
          collection_scope: 'public'
        })
      );
      expect(item.attribution.filters).toEqual(expect.objectContaining({ include_nsfw: false, scope: 'public' }));
      expect(item).not.toHaveProperty('score');
      expect(item.meme.primary_file ?? {}).not.toHaveProperty('s3_original_key');
      expect(item.meme.primary_file ?? {}).not.toHaveProperty('s3_web_video_key');
      expect(item.meme.primary_file ?? {}).not.toHaveProperty('source_object_key');
    }
  }

  expectAccessCookieSet(response: APIResponse): string {
    const accessToken = parseAccessTokenCookie(response.headers()['set-cookie']);
    expect(accessToken, 'home-feed should bootstrap a guest access cookie').toBeTruthy();
    expect(response.headers()['set-cookie']).toContain('HttpOnly');
    if (!accessToken) throw new Error('Home feed did not return an access cookie.');
    return accessToken;
  }

  expectAccessCookieNotSet(response: APIResponse) {
    expect(
      parseAccessTokenCookie(response.headers()['set-cookie']),
      'home-feed should reuse the stored guest cookie'
    ).toBeNull();
  }

  async expectNoAccessCookieStored() {
    expect(await this.accessCookie(), 'request context should start without a guest access cookie').toBeUndefined();
  }

  async expectAccessCookieStored(expectedValue: string) {
    const cookie = await this.accessCookie();
    expect(cookie, 'request context should store the guest access cookie').toBeTruthy();
    if (!cookie) throw new Error('Request context did not store the access cookie.');
    expect(cookie.value).toBe(expectedValue);
    expect(cookie.httpOnly).toBe(true);
  }

  async expectMemeInSearch(memeId: string, params: Record<string, string>) {
    const payload = await this.searchMemes(params);
    expect(payload.items.map((item) => item.meme.id)).toContain(memeId);
  }

  async expectMemeNotInSearch(memeId: string, params: Record<string, string>) {
    const payload = await this.searchMemes(params);
    expect(payload.items.map((item) => item.meme.id)).not.toContain(memeId);
  }

  async expectMemeDetail(slug: string, memeId: string) {
    const response = await this.request.get(`${this.apiBaseUrl}/api/v1/memes/slug/${slug}`);
    await expect(response).toBeOK();
    expect(await response.json()).toEqual(expect.objectContaining({ id: memeId, seo_page_slug: slug }));
  }

  async expectDualIndexProof(memeFileId: string) {
    const response = await this.request.post(`${this.apiBaseUrl}/api/v1/pipeline/search/smoke`, {
      headers: { 'X-Memexpert-Operator-Token': this.operatorToken },
      data: { meme_file_id: memeFileId }
    });
    await expect(response).toBeOK();
    expect(await response.json()).toEqual(expect.objectContaining({ both_targets_searchable: true }));
  }

  private async accessCookie() {
    const state = await this.request.storageState();
    return state.cookies.find((cookie) => cookie.name === ACCESS_COOKIE_NAME);
  }
}

export async function loginViaEmail(page: Page, apiBaseUrl: string, user: SeededE2EUser) {
  const response = await page.request.post(`${apiBaseUrl}/api/v1/auth/email/login`, {
    data: { email: user.email, password: user.password }
  });
  await expect(response).toBeOK();

  const accessToken = parseAccessTokenCookie(response.headers()['set-cookie']);
  if (!accessToken) throw new Error('Email login did not return an access cookie.');

  const frontendBaseUrl = process.env.E2E_FRONTEND_BASE_URL ?? 'http://frontend:3000';
  const frontendOrigin = new URL(frontendBaseUrl).origin;
  const origins = [...new Set([new URL(apiBaseUrl).origin, frontendOrigin])];
  await page.context().addCookies(
    origins.map((origin) => ({
      name: ACCESS_COOKIE_NAME,
      value: accessToken,
      url: origin,
      httpOnly: true,
      secure: origin.startsWith('https://'),
      sameSite: 'Lax'
    }))
  );
}

export async function removeCollectionMemberViaApi(
  page: Page,
  apiBaseUrl: string,
  input: { collectionId: string; memberUserId: string }
) {
  const response = await page.request.delete(
    `${apiBaseUrl}/api/v1/collections/${input.collectionId}/members/${input.memberUserId}`
  );
  await expect(response).toBeOK();
}

function parseAccessTokenCookie(setCookie: string | undefined): string | null {
  if (!setCookie) return null;
  const cookie = setCookie
    .split(/,(?=\s*[^;,]+=)/)
    .find((part) => part.trim().startsWith(`${ACCESS_COOKIE_NAME}=`));
  if (!cookie) return null;

  const firstPart = cookie.split(';', 1)[0].trim();
  const value = firstPart.slice(ACCESS_COOKIE_NAME.length + 1);
  return value.startsWith('"') && value.endsWith('"') ? value.slice(1, -1) : value;
}
