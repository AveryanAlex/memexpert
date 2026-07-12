import { expect, type Page } from '@playwright/test';
import type { SeededMeme } from '../helpers/seed';

export class HomePage {
  constructor(private page: Page) {}

  async goto() {
    await this.page.goto('/');
  }

  async searchFor(query: string) {
    const searchInput = this.page.getByLabel('Search memes');
    await searchInput.fill(query);
    await searchInput.press('Enter');
  }

  async expectGuestHomeFeedFallback(seededMemes: SeededMeme[]) {
    await expect(this.page.getByRole('heading', { name: 'Discover', exact: true })).toBeVisible();
    await expect(this.page.getByText('Fresh memes, ready to send.', { exact: true })).toBeVisible();
    await expect(this.page.getByText('Discover more', { exact: true })).toBeVisible();
    await expect(this.page.getByText('No home feed memes yet')).toHaveCount(0);

    const publicSeededMemes = seededMemes.filter((meme) => !meme.is_nsfw);
    await expect
      .poll(
        async () => {
          for (const meme of publicSeededMemes) {
            if (await this.page.getByRole('link', { name: `Open ${meme.title}` }).first().isVisible()) {
              return meme.title;
            }
          }
          return '';
        },
        { message: 'expected the guest home feed to show at least one seeded public meme' }
      )
      .not.toBe('');
  }
}
