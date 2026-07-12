import { expect, type Page } from '@playwright/test';
import type { SeededCollectionManagementFixture } from '../helpers/seed';

export class LibraryPage {
  constructor(private page: Page) {}

  async goto() {
    await this.page.goto('/library');
  }

  async expectFullAccountLibraryState(fixture: SeededCollectionManagementFixture) {
    await this.goto();

    await expect(this.page.getByRole('heading', { name: 'Your saved memes', exact: true })).toBeVisible();
    await expect(this.page.getByText('New collection', { exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Active save destination', exact: true })).toBeVisible();
    await expect(this.page.getByRole('combobox', { name: 'Save into', exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Collections', exact: true })).toBeVisible();
    await expect(this.page.getByRole('link', { name: fixture.collection.title, exact: true })).toBeVisible();
    await expect(this.page.getByLabel('Favorites', { exact: true }).getByRole('heading', { name: 'Favorites', exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Pins', exact: true })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Pin order', exact: true })).toBeVisible();
    await expect(this.page.getByRole('link', { name: `Open ${fixture.pinned_memes[0].title}`, exact: true }).first()).toBeVisible();
  }

  async expectGuestFullAccountActionsUnavailable() {
    await this.goto();

    await expect(this.page.getByRole('heading', { name: 'Your saved memes', exact: true })).toBeVisible();
    await expect(this.page.getByText('New collection', { exact: true })).toHaveCount(0);
    await expect(this.page.getByRole('button', { name: 'Create collection', exact: true })).toHaveCount(0);
    await expect(this.page.getByRole('heading', { name: 'Active save destination', exact: true })).toBeVisible();
    await expect(this.page.getByLabel('Favorites', { exact: true }).getByRole('heading', { name: 'Favorites', exact: true })).toBeVisible();
    await expect(this.page.getByText('Guests save into Favorites.', { exact: true })).toBeVisible();
  }

  async moveFirstPinDownAndExpectSaved() {
    await this.goto();
    await expect(this.page.getByRole('heading', { name: 'Pin order', exact: true })).toBeVisible();
    await this.page.getByRole('button', { name: 'Down', exact: true }).first().click();
    await expect(this.page.getByText('Pin order saved.', { exact: true })).toBeVisible();
  }
}
