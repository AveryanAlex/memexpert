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

  async likeAndUnlike() {
    await this.page.getByRole('button', { name: /^Like \(/ }).click();
    await expect(this.page.getByRole('button', { name: /^Unlike \(/ })).toBeVisible();
    await expect(this.page.getByText('Liked.')).toBeVisible();

    await this.page.getByRole('button', { name: /^Unlike \(/ }).click();
    await expect(this.page.getByRole('button', { name: /^Like \(/ })).toBeVisible();
    await expect(this.page.getByText('Unliked.')).toBeVisible();
  }

  async likeAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    const requestPromise = this.waitForActionPost(meme.meme_id, 'favorite');
    await this.page.getByRole('button', { name: /^Like \(/ }).click();
    expectRequestAttribution(await requestPromise, attribution, 'favorite action');

    await expect(this.page.getByRole('button', { name: /^Unlike \(/ })).toBeVisible();
    await expect(this.page.getByRole('status')).toHaveText('Liked.');
  }

  async saveAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    const requestPromise = this.waitForActionPost(meme.meme_id, 'save');
    await this.page.getByRole('button', { name: 'Save', exact: true }).click();
    expectRequestAttribution(await requestPromise, attribution, 'save action');

    await expect(this.page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible();
    await expect(this.page.getByRole('status')).toHaveText('Saved to your active collection.');
  }

  async downloadAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    await this.stubDownloadAnchors();

    const requestPromise = this.waitForActionPost(meme.meme_id, 'download');
    await this.page.getByRole('button', { name: 'Download', exact: true }).click();
    expectRequestAttribution(await requestPromise, attribution, 'download action');

    await expect(this.page.getByRole('status')).toHaveText('Download started.');
    const downloadHref = await this.lastDownloadHref();
    expect(downloadHref).toBeTruthy();
  }

  async shareToTelegramAndExpectAttribution(meme: SeededMeme, attribution: ExpectedMemeAttribution) {
    await this.stubWindowOpen();

    const requestPromise = this.waitForActionPost(meme.meme_id, 'share');
    await this.page.getByRole('button', { name: 'Share to Telegram' }).click();
    expectRequestAttribution(await requestPromise, attribution, 'share action');

    await expect(this.page.getByRole('status')).toHaveText('Opened Telegram share.');
    const shareUrl = await this.lastOpenedUrl();
    expect(shareUrl).toMatch(/^https:\/\/t\.me\/share\/url\?/);
    expect(new URL(shareUrl ?? '').searchParams.get('url')).toContain(`/memes/${meme.slug}`);
  }

  async expectPinFullAccountOnly() {
    await expect(this.page.getByText('Pin requires a full account')).toBeVisible();
    await expect(this.page.getByRole('button', { name: /^Pin$/ })).toHaveCount(0);
  }

  private waitForActionPost(memeId: string, action: 'download' | 'favorite' | 'save' | 'share') {
    return this.page.waitForRequest((request) => {
      const url = new URL(request.url());
      return request.method() === 'POST' && url.pathname === `/api/v1/memes/${memeId}/${action}`;
    });
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
