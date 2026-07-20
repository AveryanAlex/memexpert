import { expect, type Page } from '@playwright/test';
import { expectRequestAttribution, type ExpectedMemeAttribution } from '../helpers/attribution';
import type { SeededMeme } from '../helpers/seed';

export class MemeDetailPage {
  constructor(private page: Page) {}

  async goto(slug: string) {
    await this.page.goto(`/memes/${slug}`);
  }

  async expectOpen(meme: SeededMeme | { slug: string; title: string }) {
    await expect(this.page).toHaveURL((url) => url.pathname === `/memes/${meme.slug}`);
    await expect(this.page.getByRole('heading', { name: meme.title })).toBeVisible();
  }

  async expectAttributionQuery(attribution: ExpectedMemeAttribution) {
    await expect(this.page).toHaveURL((url) => {
      return url.searchParams.get('attribution_token') === attribution.token;
    });
  }

  async expectMediaLoadedThroughImgproxy(title: string) {
    const media = this.page.getByRole('img', { name: title }).first();
    await expect(media).toBeVisible();
    await expect(media).toHaveAttribute('src', /http:\/\/imgproxy:8080\/unsafe\//);
    await expect.poll(() => media.evaluate((img) => (img as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  }

  async favoriteAndUnfavorite() {
    const primaryActions = this.primaryActions();
    const favoriteButton = primaryActions.getByRole('button', { name: /^Favorite \(/ });
    await favoriteButton.click();
    await expect(favoriteButton).toHaveAttribute('aria-pressed', 'true');
    await expect(this.page.getByText('Added to favorites.', { exact: true })).toHaveCount(0);

    await favoriteButton.click();
    await expect(favoriteButton).toHaveAttribute('aria-pressed', 'false');
    await expect(this.page.getByText('Removed from favorites.', { exact: true })).toHaveCount(0);
  }

  async favoriteAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    const requestPromise = this.waitForActionPost(meme.meme_id, 'favorite');
    const primaryActions = this.primaryActions();
    await primaryActions.getByRole('button', { name: /^Favorite \(/ }).click();
    expectRequestAttribution(await requestPromise, attribution, 'favorite action');

    await expect(primaryActions.getByRole('button', { name: /^Favorite \(/ })).toHaveAttribute('aria-pressed', 'true');
    await expect(this.page.getByText('Added to favorites.', { exact: true })).toHaveCount(0);
  }

  async expectGuestSaveChooserExcludesFavorites() {
    const primaryActions = this.primaryActions();
    const saveButton = primaryActions.getByRole('button', { name: 'Save to collection', exact: true });
    await expect(saveButton).toHaveAttribute('aria-pressed', 'false');
    await saveButton.click();
    await expect(
      this.page.getByRole('button', { name: /^(?:Add to|Remove from|Saved in) Favorites/ })
    ).toHaveCount(0);
    await expect(this.page.getByText('No non-Favorites collections are available yet.', { exact: true })).toBeVisible();
    await this.page.keyboard.press('Escape');
    await expect(saveButton).toHaveAttribute('aria-pressed', 'false');
  }

  async downloadAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    await this.stubDownloadAnchors();

    const requestPromise = this.waitForActionPost(meme.meme_id, 'download');
    await this.openActionsMenu();
    await this.page.getByRole('menuitem', { name: 'Download', exact: true }).click();
    expectRequestAttribution(await requestPromise, attribution, 'download action');

    await expect(this.page.getByText('Download started.', { exact: true })).toHaveCount(0);
    const downloadHref = await this.lastDownloadHref();
    expect(downloadHref).toBeTruthy();
  }

  async shareToTelegramAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    await this.stubWindowOpen();

    const requestPromise = this.waitForActionPost(meme.meme_id, 'share');
    await this.primaryActions().getByRole('button', { name: 'Send', exact: true }).click();
    expectRequestAttribution(await requestPromise, attribution, 'share action');

    await expect(this.page.getByText('Opened Telegram share.', { exact: true })).toHaveCount(0);
    const shareUrl = await this.lastOpenedUrl();
    expect(shareUrl).toMatch(/^https:\/\/t\.me\/share\/url\?/);
    expect(new URL(shareUrl ?? '').searchParams.get('url')).toContain(`/memes/${meme.slug}`);
  }

  async expectPinUnavailableForGuest() {
    await this.openActionsMenu();
    await expect(this.page.getByRole('menuitem', { name: 'Pin', exact: true })).toHaveCount(0);
    await this.page.keyboard.press('Escape');
  }

  private waitForActionPost(memeId: string, action: 'download' | 'favorite' | 'share') {
    return this.page.waitForRequest((request) => {
      const url = new URL(request.url());
      return request.method() === 'POST' && url.pathname === `/api/v1/memes/${memeId}/${action}`;
    });
  }

  private async openActionsMenu() {
    await this.page.getByRole('button', { name: 'Meme actions', exact: true }).click();
  }

  private primaryActions() {
    return this.page.getByLabel('Primary meme actions');
  }

  private async stubWindowOpen() {
    await this.page.evaluate(() => {
      const win = window as Window & { __memexpertLastOpenedUrl?: string };
      win.__memexpertLastOpenedUrl = undefined;
      window.open = (url?: string | URL) => {
        win.__memexpertLastOpenedUrl = url?.toString();
        return null;
      };
    });
  }

  private async lastOpenedUrl() {
    return this.page.evaluate(() => (window as Window & { __memexpertLastOpenedUrl?: string }).__memexpertLastOpenedUrl ?? null);
  }

  private async stubDownloadAnchors() {
    await this.page.evaluate(() => {
      type DownloadWindow = Window & {
        __memexpertDownloadClickPatched?: boolean;
        __memexpertLastDownloadHref?: string;
      };

      const win = window as DownloadWindow;
      win.__memexpertLastDownloadHref = undefined;
      if (win.__memexpertDownloadClickPatched) return;

      const originalClick = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function click() {
        if (this.hasAttribute('download')) {
          win.__memexpertLastDownloadHref = this.href;
          return;
        }
        return originalClick.call(this);
      };
      win.__memexpertDownloadClickPatched = true;
    });
  }

  private async lastDownloadHref() {
    return this.page.evaluate(() => (window as Window & { __memexpertLastDownloadHref?: string }).__memexpertLastDownloadHref ?? null);
  }
}
