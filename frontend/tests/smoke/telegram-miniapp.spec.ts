import { expect, test, type Page } from '@playwright/test';

const telegramSdkUrl = '**/telegram-web-app.js*';

test('Telegram Mini App launch authenticates, applies shell state, and routes meme startapp', async ({ page }) => {
  const { telegramSdkRequest } = await installFakeTelegramSdk(page);

  const authResponsePromise = page.waitForResponse('**/telegram-miniapp/auth');
  await page.goto(`/#${miniAppLaunchHash()}`);

  const authResponse = await authResponsePromise;
  expect(authResponse.status()).toBe(200);
  await expect(telegramSdkRequest).resolves.toBe(true);
  await expect(authResponse.json()).resolves.not.toHaveProperty('access_token');
  await expect(page).toHaveURL(/\/memes\/smoke-test-cat-reaction$/);
  await expect(page.getByRole('heading', { name: 'Smoke test cat reaction' })).toBeVisible();
  const brand = page.locator('.app-shell-brand');
  const accountControl = page.locator('.app-shell-account');
  const miniAppShell = page.locator('.telegram-miniapp-shell');
  await expect(brand).toHaveCount(1);
  await expect(brand).toBeHidden();
  await expect(accountControl).toHaveCount(1);
  await expect(accountControl).toBeHidden();
  await expect(page.getByRole('search')).toBeVisible();
  await expect(miniAppShell).toHaveCSS('background-color', 'rgb(16, 24, 32)');

  const cookie = (await page.context().cookies()).find((item) => item.name === 'memexpert_access_token');
  expect(cookie?.value).toBe('miniapp-full');

  const shellState = await page.evaluate(() => {
    const html = document.documentElement;
    const styles = html.style;
    const calls = (window as Window & { __telegramMiniAppCalls?: { ready: number; expand: number } }).__telegramMiniAppCalls;
    return {
      htmlMiniApp: html.dataset.telegramMiniapp,
      bodyMiniApp: document.body.dataset.telegramMiniapp,
      colorScheme: html.dataset.telegramColorScheme,
      expanded: html.dataset.telegramExpanded,
      bg: styles.getPropertyValue('--tg-theme-bg-color'),
      stableHeight: styles.getPropertyValue('--tg-viewport-stable-height'),
      readyCalls: calls?.ready,
      expandCalls: calls?.expand
    };
  });

  expect(shellState).toEqual({
    htmlMiniApp: 'true',
    bodyMiniApp: 'true',
    colorScheme: 'dark',
    expanded: 'true',
    bg: '#101820',
    stableHeight: '700px',
    readyCalls: 1,
    expandCalls: 1
  });
});

test('normal web launch does not load the Telegram Mini App SDK', async ({ page }) => {
  let requestedTelegramSdk = false;
  await page.route(telegramSdkUrl, async (route) => {
    requestedTelegramSdk = true;
    await route.fulfill({ contentType: 'application/javascript', body: '' });
  });

  await page.goto('/');
  await expect(page.getByRole('link', { name: 'MemeXpert' })).toBeVisible();

  expect(requestedTelegramSdk).toBe(false);
});

function miniAppLaunchHash(): string {
  return new URLSearchParams({
    tgWebAppData: 'query_id=smoke-miniapp-init-data&user=%7B%22id%22%3A303030303%7D&auth_date=1770000000&hash=mocked',
    tgWebAppStartParam: 'meme_smoke-test-cat-reaction',
    tgWebAppThemeParams: JSON.stringify({
      bg_color: '#101820',
      secondary_bg_color: '#0b1020',
      text_color: '#f8fafc',
      button_color: '#f97316'
    }),
    tgWebAppColorScheme: 'dark'
  }).toString();
}

async function installFakeTelegramSdk(page: Page): Promise<{ telegramSdkRequest: Promise<boolean> }> {
  let resolveTelegramSdkRequest: (requested: boolean) => void = () => {};
  const telegramSdkRequest = new Promise<boolean>((resolve) => {
    resolveTelegramSdkRequest = resolve;
  });
  await page.route(telegramSdkUrl, async (route) => {
    resolveTelegramSdkRequest(true);
    await route.fulfill({
      contentType: 'application/javascript',
      body: `
        (() => {
          const launchParams = new URLSearchParams(window.location.hash.replace(/^#/, ''));
          const calls = { ready: 0, expand: 0 };
          Object.defineProperty(window, '__telegramMiniAppCalls', {
            configurable: true,
            value: calls
          });
          window.Telegram = {
            WebApp: {
              initData: launchParams.get('tgWebAppData') || '',
              initDataUnsafe: { start_param: launchParams.get('tgWebAppStartParam') || undefined },
              themeParams: JSON.parse(launchParams.get('tgWebAppThemeParams') || '{}'),
              colorScheme: launchParams.get('tgWebAppColorScheme') || null,
              viewportHeight: 720,
              viewportStableHeight: 700,
              isExpanded: false,
              ready() {
                calls.ready += 1;
              },
              expand() {
                calls.expand += 1;
                this.isExpanded = true;
              }
            }
          };
        })();
      `
    });
  });

  return { telegramSdkRequest };
}
