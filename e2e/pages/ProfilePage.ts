import { expect, type Page } from '@playwright/test';

export class ProfilePage {
  constructor(private page: Page) {}

  async goto() {
    await this.page.goto('/profile');
  }

  async expectNsfwEnabled() {
    await expect(this.page.getByText('Sensitive content is enabled.', { exact: true })).toBeVisible();
    await expect(this.page.getByText('Turn it off to filter sensitive memes from discovery again.', { exact: true })).toBeVisible();
  }

  async disableNsfw() {
    await this.page.getByRole('button', { name: 'Turn off sensitive content', exact: true }).click();
    await expect(this.page.getByText('Sensitive content stays hidden.', { exact: true })).toBeVisible();
    await expect(this.page.getByRole('status')).toHaveText('Sensitive content is hidden again.');
  }

  async expectFullAccountState() {
    await this.goto();

    await expect(this.page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
    await expect(this.page.getByText('Connected account', { exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Telegram', exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Preferences', exact: true })).toBeVisible();
    await expect(this.page.getByRole('combobox', { name: 'Profile language', exact: true })).toBeVisible();
    await expect(this.page.getByText(/Sensitive content (stays hidden|is enabled)\./)).toBeVisible();
    await this.page.getByText('Interaction stats', { exact: true }).click();
    for (const label of ['Viewed', 'Sent', 'Saved', 'Downloaded', 'Days active']) {
      await expect(this.page.getByText(label, { exact: true }).first()).toBeVisible();
    }
    await expect(this.page.getByRole('link', { name: 'Open Saved', exact: true })).toBeVisible();
  }
}
