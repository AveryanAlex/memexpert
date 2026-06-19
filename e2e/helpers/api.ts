import { expect, type APIRequestContext, type Page } from '@playwright/test';
import type { SeededE2EUser } from './seed';

const ACCESS_COOKIE_NAME = 'memexpert_access_token';

export interface SearchItem {
  meme: { id: string; seo_page_slug: string | null; is_nsfw: boolean };
}

export interface SearchPayload {
  items: SearchItem[];
  total: number;
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
