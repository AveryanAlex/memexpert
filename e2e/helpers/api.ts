import { expect, type APIRequestContext } from '@playwright/test';

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
