import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/app';
import { seededByCategory } from '../helpers/seed';

test('Telegram Mini App shell applies host state and deep-links to a seeded meme', async ({ app, page, seed }) => {
  const meme = seededByCategory(seed, 'cat');

  await installFakeTelegramWebApp(page, `meme_${meme.slug}`);
  await page.goto('/');

  await expectTelegramShellState(page);
  await expect(page).toHaveURL((url) => url.pathname === `/memes/${meme.slug}`);
  await app.detail.expectOpen(meme);
});

async function installFakeTelegramWebApp(page: Page, startParam: string) {
  await page.addInitScript((input: { startParam: string }) => {
    type FakeTelegramWebApp = {
      initData: string;
      initDataUnsafe: { start_param: string };
      themeParams: Record<string, string>;
      colorScheme: 'dark';
      viewportHeight: number;
      viewportStableHeight: number;
      isExpanded: boolean;
      ready: () => void;
      expand: () => void;
    };
    type FakeTelegramWindow = Window & {
      Telegram?: { WebApp: FakeTelegramWebApp };
      __memexpertTelegramHostCalls?: { ready: number; expand: number };
    };

    const win = window as FakeTelegramWindow;
    win.__memexpertTelegramHostCalls = { ready: 0, expand: 0 };

    const webApp: FakeTelegramWebApp = {
      initData: '',
      initDataUnsafe: { start_param: input.startParam },
      themeParams: {
        bg_color: '#101820',
        button_color: '#f97316',
        text_color: '#f8fafc'
      },
      colorScheme: 'dark',
      viewportHeight: 612,
      viewportStableHeight: 590,
      isExpanded: false,
      ready: () => {
        win.__memexpertTelegramHostCalls!.ready += 1;
      },
      expand: () => {
        win.__memexpertTelegramHostCalls!.expand += 1;
        webApp.isExpanded = true;
      }
    };

    win.Telegram = { WebApp: webApp };
  }, { startParam });
}

async function expectTelegramShellState(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(() => {
        const html = document.documentElement;
        const body = document.body;
        const calls = (window as Window & { __memexpertTelegramHostCalls?: { ready: number; expand: number } })
          .__memexpertTelegramHostCalls;

        return {
          htmlClass: html.classList.contains('telegram-miniapp'),
          bodyClass: body.classList.contains('telegram-miniapp'),
          htmlMiniApp: html.getAttribute('data-telegram-miniapp'),
          bodyMiniApp: body.getAttribute('data-telegram-miniapp'),
          colorScheme: html.getAttribute('data-telegram-color-scheme'),
          htmlExpanded: html.getAttribute('data-telegram-expanded'),
          bodyExpanded: body.getAttribute('data-telegram-expanded'),
          themeBg: html.style.getPropertyValue('--tg-theme-bg-color'),
          themeText: body.style.getPropertyValue('--tg-theme-text-color'),
          viewportHeight: html.style.getPropertyValue('--tg-viewport-height'),
          viewportStableHeight: html.style.getPropertyValue('--tg-viewport-stable-height'),
          readyCalls: calls?.ready ?? 0,
          expandCalls: calls?.expand ?? 0
        };
      })
    )
    .toEqual({
      htmlClass: true,
      bodyClass: true,
      htmlMiniApp: 'true',
      bodyMiniApp: 'true',
      colorScheme: 'dark',
      htmlExpanded: 'true',
      bodyExpanded: 'true',
      themeBg: '#101820',
      themeText: '#f8fafc',
      viewportHeight: '612px',
      viewportStableHeight: '590px',
      readyCalls: 1,
      expandCalls: 1
    });
}
