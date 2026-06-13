import type { Page } from '@playwright/test';

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

  async expectGuestCollectionCreationUnavailable() {
    await this.page.getByText('Connect Telegram to create custom collections and collaborate.').waitFor();
    await this.page.getByRole('button', { name: 'Create collection' }).waitFor({ state: 'detached' });
  }
}
