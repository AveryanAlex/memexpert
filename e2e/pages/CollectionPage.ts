import { expect, type Page } from '@playwright/test';
import type { SeededCollectionManagementFixture, SeededMeme } from '../helpers/seed';

export class CollectionPage {
  constructor(private page: Page) {}

  async goto(collectionId: string) {
    await this.page.goto(`/collection/${collectionId}`);
  }

  async joinInvite(invitePath: string, collectionId: string) {
    await this.page.goto(invitePath);
    await expect(this.page.getByRole('heading', { name: 'Join a meme collection' })).toBeVisible();
    await this.page.getByRole('button', { name: 'Join collection' }).click();
    await expect(this.page).toHaveURL((url) => url.pathname === `/collection/${collectionId}`);
  }

  async expectOwnerControls(fixture: SeededCollectionManagementFixture) {
    await this.expectOpen(fixture);
    await this.openManagement();
    await expect(this.page.getByRole('heading', { name: 'Invite link' })).toBeVisible();
    await expect(this.page.getByRole('button', { name: 'Create invite' })).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Members' })).toBeVisible();
    await expect(this.page.getByText('Owners can update non-owner roles or remove non-owner members.')).toBeVisible();
    await expect(this.page.getByRole('heading', { name: 'Invites' })).toBeVisible();
    await expect(this.page.getByText('E2E viewer invite')).toBeVisible();
    await expect(this.page.getByRole('button', { name: 'Revoke' })).toBeVisible();
  }

  async expectViewerGuidance(fixture: SeededCollectionManagementFixture) {
    await this.expectOpen(fixture);
    await this.openManagement();
    await expect(this.page.getByText('Your access is view-only.')).toBeVisible();
    await expect(
      this.page.getByText('You can view this collection, but member and invite management require editor or owner access.').first()
    ).toBeVisible();
    await expect(this.page.getByRole('button', { name: 'Create invite' })).toHaveCount(0);
    await expect(this.page.getByRole('button', { name: 'Update role' })).toHaveCount(0);
  }

  async updateMemberRole(memberUserId: string, role: 'editor' | 'viewer') {
    await this.openManagement();
    await this.page.getByLabel(`Role for ${shortId(memberUserId)}`).selectOption(role);
    await this.page.getByRole('button', { name: 'Update role' }).click();
    await expect(this.page.getByText('Member role updated.')).toBeVisible();
    await expect(this.page.getByLabel(`Role for ${shortId(memberUserId)}`)).toHaveValue(role);
  }

  async expectEditorControls(fixture: SeededCollectionManagementFixture) {
    await this.expectOpen(fixture);
    await this.openManagement();
    await expect(this.page.getByRole('heading', { name: 'Invite link' })).toBeVisible();
    await expect(this.page.getByRole('button', { name: 'Create invite' })).toBeVisible();
    await expect(
      this.page.getByText('Editors can create and revoke invite links. Member role changes and removals require owner access.').first()
    ).toBeVisible();
    await expect(this.page.getByRole('button', { name: 'Update role' })).toHaveCount(0);
  }

  async expectSavedMemeVisible(meme: SeededMeme) {
    await expect(this.page.getByRole('link', { name: `Open ${meme.title}` }).first()).toBeVisible();
  }

  private async expectOpen(fixture: SeededCollectionManagementFixture) {
    await expect(this.page).toHaveURL((url) => url.pathname === `/collection/${fixture.collection.id}`);
    await expect(this.page.getByRole('heading', { name: fixture.collection.title })).toBeVisible();
  }

  private async openManagement() {
    const management = this.page.getByText('Manage collection', { exact: true });
    const details = this.page.locator('details').filter({ has: management });
    const isOpen = await details.evaluate((element) => (element as HTMLDetailsElement).open);
    if (!isOpen) {
      await details.locator('summary').click();
    }
  }
}

function shortId(value: string): string {
  return value.length <= 12 ? value : `${value.slice(0, 8)}...${value.slice(-4)}`;
}
