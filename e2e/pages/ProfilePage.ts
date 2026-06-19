import { expect, type Page } from '@playwright/test';

export class ProfilePage {
  constructor(private page: Page) {}

  async goto() {
    await this.page.goto('/profile');
  }

  async expectNsfwEnabled() {
    await expect(this.page.getByText('NSFW search is enabled.')).toBeVisible();
    await expect(this.page.getByText('Search can include NSFW memes')).toBeVisible();
  }

  async disableNsfw() {
    await this.page.getByRole('button', { name: 'Turn off NSFW' }).click();
    await expect(this.page.getByText('NSFW stays hidden.')).toBeVisible();
    await expect(this.page.getByText('NSFW is hidden again.')).toBeVisible();
  }

  async moveFirstPinDownAndExpectSaved() {
    await this.goto();
    await expect(this.page.getByRole('heading', { name: 'Pin order' })).toBeVisible();
    await this.page.getByRole('button', { name: 'Down' }).first().click();
    await expect(this.page.getByText('Pin order saved.')).toBeVisible();
  }
}
