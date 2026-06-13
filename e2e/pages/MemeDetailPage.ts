import { expect, type Page } from '@playwright/test';
import type { SeededMeme } from '../helpers/seed';

export class MemeDetailPage {
  constructor(private page: Page) {}

  async goto(slug: string) {
    await this.page.goto(`/memes/${slug}`);
  }

  async expectOpen(meme: SeededMeme | { slug: string; title: string }) {
    await expect(this.page).toHaveURL(new RegExp(`/memes/${meme.slug}$`));
    await expect(this.page.getByRole('heading', { name: meme.title })).toBeVisible();
  }

  async expectMediaLoadedThroughImgproxy(title: string) {
    const media = this.page.getByRole('img', { name: title }).first();
    await expect(media).toBeVisible();
    await expect(media).toHaveAttribute('src', /http:\/\/imgproxy:8080\/unsafe\//);
    await expect.poll(() => media.evaluate((img) => (img as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  }

  async likeAndUnlike() {
    await this.page.getByRole('button', { name: /^Like \(/ }).click();
    await expect(this.page.getByRole('button', { name: /^Unlike \(/ })).toBeVisible();
    await expect(this.page.getByText('Liked.')).toBeVisible();

    await this.page.getByRole('button', { name: /^Unlike \(/ }).click();
    await expect(this.page.getByRole('button', { name: /^Like \(/ })).toBeVisible();
    await expect(this.page.getByText('Unliked.')).toBeVisible();
  }

  async expectPinFullAccountOnly() {
    await expect(this.page.getByText('Pin requires a full account')).toBeVisible();
    await expect(this.page.getByRole('button', { name: /^Pin$/ })).toHaveCount(0);
  }
}
