import { expect, type Page } from '@playwright/test';
import type { SeededMeme } from '../helpers/seed';

export class SearchPage {
  constructor(private page: Page) {}

  async applyFilters(input: { query: string; tag: string; mediaType: string; language: string; includeNsfw: boolean }) {
    const searchInput = this.page.getByLabel('Search text');
    await searchInput.fill(input.query);
    await this.page.getByLabel('Media type').selectOption(input.mediaType);
    await this.page.getByLabel('Language').selectOption(input.language);
    await this.page.getByLabel('NSFW').selectOption(String(input.includeNsfw));
    await this.page.getByLabel('Tags / categories').fill(input.tag);
    await searchInput.press('Enter');
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

  async expectResultVisible(meme: SeededMeme | { title: string }) {
    await expect(this.page.getByRole('link', { name: `Open ${meme.title}` }).first()).toBeVisible();
  }

  async expectResultHidden(meme: SeededMeme) {
    await expect(this.page.getByRole('link', { name: `Open ${meme.title}` })).toHaveCount(0);
  }

  async openResult(meme: SeededMeme | { title: string }) {
    await this.page.getByRole('link', { name: `Open ${meme.title}` }).first().click();
  }
}
