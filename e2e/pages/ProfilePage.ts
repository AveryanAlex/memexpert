import { expect, type Page } from '@playwright/test';
import type { SeededCollectionManagementFixture } from '../helpers/seed';

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

  async expectFullAccountProfileState(fixture: SeededCollectionManagementFixture) {
    await this.goto();

    await expect(this.page.getByRole('heading', { name: 'Your meme shelf.', exact: true })).toBeVisible();
    await expect(this.page.getByText('Connected profile', { exact: true })).toBeVisible();
    await expect(
      this.page.getByText('Favorites, saves, pins, and active collection follow this connected account.', { exact: true })
    ).toBeVisible();

    await expect(this.page.getByRole('heading', { name: 'Interaction stats', exact: true })).toBeVisible();
    await expect(this.page.getByText('Counts come from your recorded meme interaction history.', { exact: true })).toBeVisible();
    for (const label of ['Viewed', 'Sent', 'Saved', 'Downloaded', 'Days active']) {
      await expect(this.page.getByText(label, { exact: true }).first()).toBeVisible();
    }

    await expect(this.page.getByRole('heading', { name: 'Account settings', exact: true })).toBeVisible();
    await expect(
      this.page.getByText('Current backend account state. Unsupported web mutations are shown honestly.', { exact: true })
    ).toBeVisible();
    await expect(this.page.getByText(fixture.owner.email, { exact: true })).toBeVisible();
    await expect(this.page.getByText('Verified', { exact: true })).toBeVisible();
    await expect(this.page.getByText('Password set', { exact: true })).toBeVisible();
    await expect(this.page.getByRole('combobox', { name: 'Profile language', exact: true })).toBeVisible();
    await expect(this.page.getByText(/NSFW (stays hidden\.|search is enabled\.)/)).toBeVisible();

    await expect(this.page.getByRole('heading', { name: 'Active save collection', exact: true })).toBeVisible();
    await expect(this.page.getByRole('combobox', { name: 'Save into', exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Collections', exact: true })).toBeVisible();
    await expect(this.page.getByRole('link', { name: fixture.collection.title, exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Favorites', exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Pinned memes', exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Pin order', exact: true })).toBeVisible();
    await expect(this.page.getByRole('link', { name: `Open ${fixture.pinned_memes[0].title}`, exact: true }).first()).toBeVisible();
  }

  async moveFirstPinDownAndExpectSaved() {
    await this.goto();
    await expect(this.page.getByRole('heading', { name: 'Pin order' })).toBeVisible();
    await this.page.getByRole('button', { name: 'Down' }).first().click();
    await expect(this.page.getByText('Pin order saved.')).toBeVisible();
  }
}
