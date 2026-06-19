import { expect, type Page } from '@playwright/test';
import { expectedAttributionFromHref, type ExpectedMemeAttribution } from '../helpers/attribution';
import type { SeededMeme } from '../helpers/seed';

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
    const searchForm = this.page.locator('form').filter({ has: this.page.getByLabel('Search text') });
    await searchForm.getByLabel('Search text').fill(input.query);
    await this.page.getByLabel('Search scope').selectOption('collections');
    for (const title of input.collectionTitles) {
      await this.page.locator('label').filter({ hasText: title }).getByRole('checkbox').check();
    }
    await searchForm.getByRole('button', { name: 'Search', exact: true }).click();
  }

  async applyFilters(input: { query: string; tag: string; mediaType: string; language: string; includeNsfw: boolean }) {
    const searchInput = this.page.getByLabel('Search text');
    await searchInput.fill(input.query);
    await this.page.getByLabel('Media type').selectOption(input.mediaType);
    await this.page.getByLabel('Language').selectOption(input.language);
    await this.page.getByLabel('NSFW').selectOption(String(input.includeNsfw));
    await this.page.getByLabel('Tags / categories').fill(input.tag);
    await searchInput.press('Enter');
  }

  async cancelNsfwOptIn() {
    await expect(this.page.getByRole('dialog', { name: 'Include NSFW results?' })).toBeVisible();
    await this.page.getByRole('button', { name: 'Cancel' }).click();
    await expect(this.page.getByRole('dialog', { name: 'Include NSFW results?' })).toHaveCount(0);
    await expect(this.page.getByLabel('NSFW')).toHaveValue('false');
  }

  async confirmNsfwOptIn() {
    await expect(this.page.getByRole('dialog', { name: 'Include NSFW results?' })).toBeVisible();
    await this.page.getByRole('button', { name: 'Confirm and search' }).click();
    await expect(this.page.getByRole('dialog', { name: 'Include NSFW results?' })).toHaveCount(0);
  }

  async expectNsfwUrlRequestNote() {
    await expect(this.page.getByText('NSFW was requested in the URL')).toBeVisible();
  }

  async expectNoNsfwOptInPrompt() {
    await expect(this.page.getByRole('dialog', { name: 'Include NSFW results?' })).toHaveCount(0);
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

  async attributionForResult(meme: SeededMeme | { title: string }): Promise<ExpectedMemeAttribution> {
    const link = this.page.getByRole('link', { name: `Open ${meme.title}` }).first();
    const href = await link.getAttribute('href');
    if (!href) throw new Error(`Search result for ${meme.title} did not include a detail href.`);
    return expectedAttributionFromHref(href, this.page.url());
  }
}
