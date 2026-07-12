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
      return (
        url.searchParams.get('attribution_request_id') === attribution.requestId &&
        url.searchParams.get('attribution_impression_id') === attribution.impressionId &&
        url.searchParams.get('attribution_source_algorithm') === attribution.sourceAlgorithm &&
        url.searchParams.get('attribution_surface') === attribution.surface &&
        url.searchParams.get('attribution_rank') === String(attribution.rank)
      );
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
    await primaryActions.getByRole('button', { name: /^Favorite \(/ }).click();
    await expect(primaryActions.getByRole('button', { name: /^Favorited \(/ })).toBeVisible();
    await expect(this.page.getByRole('status')).toHaveText('Added to favorites.');

    await primaryActions.getByRole('button', { name: /^Favorited \(/ }).click();
    await expect(primaryActions.getByRole('button', { name: /^Favorite \(/ })).toBeVisible();
    await expect(this.page.getByRole('status')).toHaveText('Removed from favorites.');
  }

  async favoriteAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    const requestPromise = this.waitForActionPost(meme.meme_id, 'favorite');
    const primaryActions = this.primaryActions();
    await primaryActions.getByRole('button', { name: /^Favorite \(/ }).click();
    expectRequestAttribution(await requestPromise, attribution, 'favorite action');

    await expect(primaryActions.getByRole('button', { name: /^Favorited \(/ })).toBeVisible();
    await expect(this.page.getByRole('status')).toHaveText('Added to favorites.');
  }

  async saveAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    const requestPromise = this.waitForActionPost(meme.meme_id, 'save');
    const primaryActions = this.primaryActions();
    await primaryActions.getByRole('button', { name: 'Save', exact: true }).click();
    expectRequestAttribution(await requestPromise, attribution, 'save action');

    await expect(primaryActions.getByRole('button', { name: 'Saved', exact: true })).toBeVisible();
    await expect(this.page.getByRole('status')).toHaveText('Saved to your active collection.');
  }

  async downloadAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    await this.stubDownloadAnchors();

    const requestPromise = this.waitForActionPost(meme.meme_id, 'download');
    await this.openActionsMenu();
    await this.page.getByRole('menuitem', { name: 'Download', exact: true }).click();
    expectRequestAttribution(await requestPromise, attribution, 'download action');

    await expect(this.page.getByRole('status')).toHaveText('Download started.');
    const downloadHref = await this.lastDownloadHref();
    expect(downloadHref).toBeTruthy();
  }

  async shareToTelegramAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    await this.stubWindowOpen();

    const requestPromise = this.waitForActionPost(meme.meme_id, 'share');
    await this.primaryActions().getByRole('button', { name: 'Send', exact: true }).click();
    expectRequestAttribution(await requestPromise, attribution, 'share action');

    await expect(this.page.getByRole('status')).toHaveText('Opened Telegram share.');
    const shareUrl = await this.lastOpenedUrl();
    expect(shareUrl).toMatch(/^https:\/\/t\.me\/share\/url\?/);
    expect(new URL(shareUrl ?? '').searchParams.get('url')).toContain(`/memes/${meme.slug}`);
  }

  async expectPinUnavailableForGuest() {
    await this.openActionsMenu();
    await expect(this.page.getByRole('menuitem', { name: 'Pin', exact: true })).toHaveCount(0);
    await this.page.keyboard.press('Escape');
  }

  private waitForActionPost(memeId: string, action: 'download' | 'favorite' | 'save' | 'share') {
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
