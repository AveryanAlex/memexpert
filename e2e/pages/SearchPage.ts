import { expect, type Page, type Request } from '@playwright/test';
import { expectRequestAttribution, expectedAttributionFromHref, type ExpectedMemeAttribution } from '../helpers/attribution';
import type { SeededMeme } from '../helpers/seed';

type SearchResultTelemetryAction = 'detail-click' | 'impression';

export class SearchPage {
  constructor(private page: Page) {}

  async gotoFilters(input: { query: string; tag: string; mediaType: string; language: string; includeNsfw: boolean }) {
    const params = new URLSearchParams({
      q: input.query,
      tags: input.tag,
      media_type: input.mediaType,
      language: input.language,
      include_nsfw: String(input.includeNsfw)
    });
    await this.page.goto(`/search?${params.toString()}`);
  }

  async searchCollections(input: { query: string; collectionTitles: string[] }) {
    await this.page.goto('/search');
    const searchForm = this.searchForm();
    await searchForm.getByRole('searchbox', { name: 'Search memes', exact: true }).fill(input.query);
    await searchForm.getByRole('button', { name: /^Filters/ }).click();
    const filters = this.filtersDialog();
    await filters.getByRole('radio', { name: /Specific collections/i }).check();
    for (const title of input.collectionTitles) {
      await filters.locator('label').filter({ hasText: title }).getByRole('checkbox').check();
    }
    await filters.getByRole('button', { name: 'Show results', exact: true }).click();
  }

  async applyFilters(input: { query: string; tag: string; mediaType: string; language: string; includeNsfw: boolean }) {
    const searchForm = this.searchForm();
    const searchInput = searchForm.getByRole('searchbox', { name: 'Search memes', exact: true });
    await searchInput.fill(input.query);
    await searchForm.getByRole('button', { name: /^Filters/ }).click();
    const filters = this.filtersDialog();
    await filters.getByLabel('Media type').selectOption(input.mediaType);
    await filters.getByLabel('Language').selectOption(input.language);
    await filters.getByLabel('Sensitive content').selectOption(String(input.includeNsfw));
    await filters.getByLabel('Tags or categories').fill(input.tag);
    await filters.getByRole('button', { name: 'Show results', exact: true }).click();
  }

  async cancelNsfwOptIn() {
    await expect(this.page.getByRole('dialog', { name: 'Include sensitive results?' })).toBeVisible();
    await this.page.getByRole('button', { name: 'Cancel' }).click();
    await expect(this.page.getByRole('dialog', { name: 'Include sensitive results?' })).toHaveCount(0);
  }

  async confirmNsfwOptIn() {
    await expect(this.page.getByRole('dialog', { name: 'Include sensitive results?' })).toBeVisible();
    await this.page.getByRole('button', { name: 'Confirm and search' }).click();
    await expect(this.page.getByRole('dialog', { name: 'Include sensitive results?' })).toHaveCount(0);
  }

  async expectNsfwUrlRequestNote() {
    await expect(this.page.getByText(/Sensitive results are requested in this link/)).toBeVisible();
  }

  async expectNoNsfwOptInPrompt() {
    await expect(this.page.getByRole('dialog', { name: 'Include sensitive results?' })).toHaveCount(0);
  }

  async expectUrlFilters(input: { query: string; tag: string; mediaType: string; language: string; includeNsfw: boolean }) {
    await expect(this.page).toHaveURL((url) => {
      return (
        url.pathname === '/search' &&
        url.searchParams.get('q') === input.query &&
        url.searchParams.get('tags') === input.tag &&
        url.searchParams.get('media_type') === input.mediaType &&
        url.searchParams.get('language') === input.language &&
        url.searchParams.get('include_nsfw') === String(input.includeNsfw)
      );
    });
  }

  async expectCollectionScopeUrl(input: { query: string; requiredCollectionId: string; minimumCollectionIds: number }) {
    await expect(this.page).toHaveURL((url) => {
      const collectionIds = url.searchParams.getAll('collection_ids');
      return (
        url.pathname === '/search' &&
        url.searchParams.get('q') === input.query &&
        url.searchParams.get('scope') === 'collections' &&
        collectionIds.includes(input.requiredCollectionId) &&
        collectionIds.length >= input.minimumCollectionIds
      );
    });
  }

  async expectResultVisible(meme: SeededMeme | { title: string }) {
    await expect(this.page.getByRole('link', { name: `Open ${meme.title}` }).first()).toBeVisible();
  }

  async expectResultHidden(meme: SeededMeme) {
    await expect(this.page.getByRole('link', { name: `Open ${meme.title}` })).toHaveCount(0);
  }

  async openResult(meme: SeededMeme | { title: string }) {
    await this.page.getByRole('link', { name: `Open ${meme.title}` }).first().click();
  }

  async scrollResultIntoView(meme: SeededMeme | { title: string }) {
    await this.page.getByRole('link', { name: `Open ${meme.title}` }).first().scrollIntoViewIfNeeded();
  }

  waitForResultImpressionPost(meme: SeededMeme): Promise<Request> {
    return this.waitForResultTelemetryPost(meme, 'impression');
  }

  waitForResultDetailClickPost(meme: SeededMeme): Promise<Request> {
    return this.waitForResultTelemetryPost(meme, 'detail-click');
  }

  async expectResultImpressionAttribution(request: Promise<Request>, expected: ExpectedMemeAttribution) {
    expectRequestAttribution(await request, expected, 'search result impression');
  }

  async expectResultDetailClickAttribution(request: Promise<Request>, expected: ExpectedMemeAttribution) {
    expectRequestAttribution(await request, expected, 'search result detail-click');
  }

  async attributionForResult(meme: SeededMeme | { title: string }): Promise<ExpectedMemeAttribution> {
    const link = this.page.getByRole('link', { name: `Open ${meme.title}` }).first();
    const href = await link.getAttribute('href');
    if (!href) throw new Error(`Search result for ${meme.title} did not include a detail href.`);
    return expectedAttributionFromHref(href, this.page.url());
  }

  private waitForResultTelemetryPost(meme: SeededMeme, action: SearchResultTelemetryAction): Promise<Request> {
    return this.page.waitForRequest((request) => {
      const url = new URL(request.url());
      return request.method() === 'POST' && url.pathname === `/api/v1/memes/${meme.meme_id}/${action}`;
    });
  }

  private searchForm() {
    return this.page.locator('form#search-results-form');
  }

  private filtersDialog() {
    return this.page.getByRole('dialog', { name: 'Filters', exact: true });
  }
}
