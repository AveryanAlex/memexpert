import { createHash } from 'node:crypto';
import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test';

const adminFixture = {
  memeId: '1cb7b083-dc9f-45a6-9e4c-3dc497651a04',
  mediaFileId: '1cb7b083-dc9f-45a6-9e4c-3dc497651a05',
  readyAccountId: '1cb7b083-dc9f-45a6-9e4c-3dc497651a06',
  quickAddedSourceId: '1cb7b083-dc9f-45a6-9e4c-3dc497651a11'
};
test.beforeEach(async ({ page, baseURL }, testInfo) => {
  await signInAsAdmin(page, baseURL, testInfo);
});

test('admin shell exposes task navigation and actionable attention cards', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await gotoAdmin(page, '/admin');

  await expect(page.getByRole('heading', { name: 'What needs attention?' })).toBeVisible();
  const navigation = page.getByRole('navigation', { name: 'Admin navigation' });
  await expect(navigation).toBeVisible();
  await expect(navigation.getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'page');
  for (const label of ['Sources', 'Moderation', 'Blocked patterns', 'SEO', 'Templates', 'Telegram accounts', 'Back to catalog']) {
    await expect(navigation.getByRole('link', { name: label })).toBeVisible();
  }

  await expect(page.getByRole('link', { name: /Open reports/ })).toContainText('1');
  await expect(page.getByRole('link', { name: /Sources need attention/ })).toContainText('2');
  await expect(page.getByText('1 need an account · 2 stale · 3 pending suggestions')).toBeVisible();
  await expect(page.getByRole('link', { name: /Telegram accounts need attention/ })).toContainText('1');
  await expect(page.getByText('1 ready')).toBeVisible();
});

test('admin adds a public Telegram source through the selected ready account and pauses that source', async ({ page }) => {
  await gotoAdmin(page, '/admin/sources');

  await expect(page.getByRole('heading', { name: 'Sources', level: 1 })).toBeVisible();
  await expect(page.getByText(/Reddit crawler support is unavailable/)).toBeVisible();
  await expect(page.getByText(/VK crawler support is unavailable/)).toBeVisible();
  const quickAddForm = page.locator('form[action="?/addSourceByReference"]');
  await expect(quickAddForm).toHaveCount(1);
  await expect(quickAddForm.getByLabel('Channel link or @handle')).toBeVisible();
  const accountSelect = quickAddForm.getByRole('combobox', { name: 'Telegram account' });
  await expect(accountSelect).toHaveValue(adminFixture.readyAccountId);
  await expect(accountSelect).toContainText('Meme desk account');
  await expect(accountSelect).not.toContainText('Rate-limited account');

  const referenceInput = quickAddForm.getByLabel('Channel link or @handle');
  const telegramSuggestion = page.locator('article').filter({ hasText: 'https://t.me/pizza_memes' });
  await telegramSuggestion.getByRole('button', { name: 'Add this source' }).click();
  await expect(referenceInput).toHaveValue('https://t.me/pizza_memes');
  await expect(referenceInput).toBeFocused();
  await quickAddForm.getByRole('button', { name: 'Cancel suggestion' }).click();
  await expect(referenceInput).toHaveValue('');
  await expect(referenceInput).toBeFocused();

  await referenceInput.fill('@fresh_public_channel');
  await quickAddForm.getByRole('button', { name: 'Add source', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Telegram source added and ready to fetch.');
  await page.reload();
  await waitForAdminHydration(page);

  const addedSource = page.locator('article').filter({ has: page.getByRole('heading', { name: 'Fresh Public Channel' }) });
  await expect(addedSource.getByText('@fresh_public_channel', { exact: true })).toBeVisible();
  await expect(addedSource.getByText(/Account: Meme desk account/)).toBeVisible();

  const diagnostics = await openDisclosure(addedSource, 'Diagnostics');
  await expect(diagnostics.getByText('Source ID', { exact: true })).toBeVisible();
  await expect(diagnostics.getByText(adminFixture.quickAddedSourceId, { exact: true })).toBeVisible();
  await expect(diagnostics.getByText('Platform ID', { exact: true })).toBeVisible();

  await addedSource.getByRole('button', { name: 'Pause' }).click();
  await expect(page.getByRole('status')).toContainText('Source paused.');
  await page.reload();
  await waitForAdminHydration(page);
  const pausedSource = page.locator('article').filter({ has: page.getByRole('heading', { name: 'Fresh Public Channel' }) });
  await expect(pausedSource.getByText('Paused', { exact: true })).toBeVisible();
});

test('Telegram accounts use operator terminology while diagnostics and advanced controls stay disclosed', async ({ page }) => {
  await gotoAdmin(page, '/admin/telegram');

  await expect(page.getByRole('heading', { name: 'Telegram accounts', level: 1 })).toBeVisible();
  await expect(page.getByText(/QR is the quickest option/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Connect with QR' })).toBeVisible();
  await expect(page.getByLabel('Phone number')).not.toBeVisible();
  const account = page.locator('article').filter({ has: page.getByRole('heading', { name: 'Meme desk account' }) });
  await expect(account.getByText('@meme_ops', { exact: true })).toBeVisible();
  await expect(account.getByLabel('Account status: Ready')).toBeVisible();
  await expect(account.getByText('Account ID', { exact: true })).not.toBeVisible();
  await expect(account.getByText('Maximum requests per second', { exact: true })).not.toBeVisible();

  const diagnostics = await openDisclosure(account, 'Diagnostics');
  await expect(diagnostics.getByText('Account ID', { exact: true })).toBeVisible();
  await expect(diagnostics.getByText('Technical account name', { exact: true })).toBeVisible();

  const advancedSettings = await openDisclosure(account, 'Advanced settings');
  await expect(advancedSettings.getByLabel('Maximum requests per second')).toBeVisible();

  const floodWaitAccount = page.locator('article').filter({ has: page.getByRole('heading', { name: 'Rate-limited account' }) });
  await expect(floodWaitAccount.getByLabel('Account status: Temporarily rate-limited')).toBeVisible();
  await expect(floodWaitAccount.getByRole('button', { name: /Validate account|Enable account|Resume account/ })).toHaveCount(0);
  await expect(floodWaitAccount.getByText(/No Telegram action is available while this account is rate-limited/)).toBeVisible();

  await expect(page.locator('body')).not.toContainText(/StringSession|encrypted_string_session|attempt[_ ]?id/i);
});

test('moderation renders private admin media through the authenticated proxy and leads to meme review', async ({ page, request, baseURL }) => {
  const privateMediaUrl = `${baseURL ?? 'http://127.0.0.1:4174'}/api/v1/media/files/${adminFixture.mediaFileId}/preview`;
  const anonymousMediaResponse = await request.get(privateMediaUrl, { maxRedirects: 0 });
  expect(anonymousMediaResponse.status()).toBe(404);

  const mediaProxyResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === `/api/v1/media/files/${adminFixture.mediaFileId}/preview` && response.status() === 307
  );
  await gotoAdmin(page, '/admin/moderation');

  await expect(page.getByRole('heading', { name: 'Reports needing a decision' })).toBeVisible();
  const preview = page.getByRole('img', { name: 'Preview for Spam report' });
  await preview.scrollIntoViewIfNeeded();
  await expect(preview).toBeVisible();
  await mediaProxyResponse;
  await page.getByRole('link', { name: 'Open full meme detail' }).click();

  await expect(page).toHaveURL(new RegExp(`/admin/memes/${adminFixture.memeId}$`));
  await expect(page.getByRole('heading', { name: 'Review meme' })).toBeVisible();
  await expect(page.getByRole('img', { name: 'Admin meme preview' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Back to moderation' })).toBeVisible();
});

test('blocked patterns keep raw matching and danger controls disclosed', async ({ page }) => {
  await gotoAdmin(page, '/admin/moderation/patterns');

  await expect(page.getByRole('heading', { name: 'Blocked media patterns' })).toBeVisible();
  const pattern = page.locator('article').filter({ has: page.getByRole('heading', { name: 'Spam' }) });
  const rawHashDetail = pattern.locator('dt').filter({ hasText: 'Raw perceptual hash' });
  const deactivateConfirmation = pattern.getByText('Type DEACTIVATE to confirm', { exact: true });
  await expect(rawHashDetail).not.toBeVisible();
  await expect(deactivateConfirmation).not.toBeVisible();

  const patternDetails = await openDisclosure(pattern, 'Pattern details and editing');
  await expect(patternDetails.locator('dt').filter({ hasText: 'Raw perceptual hash' })).toBeVisible();
  const patternLifecycle = await openDisclosure(pattern, 'Pattern lifecycle and deletion');
  await expect(patternLifecycle.getByText('Type DEACTIVATE to confirm', { exact: true })).toBeVisible();
});

test('content routes redirect to SEO and keep templates reachable from admin navigation', async ({ page }) => {
  await gotoAdmin(page, '/admin/content');

  await expect(page).toHaveURL(/\/admin\/content\/seo$/);
  await expect(page.getByRole('heading', { name: 'SEO review queue' })).toBeVisible();
  await page.getByRole('navigation', { name: 'Admin navigation' }).getByRole('link', { name: 'Templates' }).click();
  await expect(page).toHaveURL(/\/admin\/content\/templates$/);
  await expect(page.getByRole('heading', { name: 'Meme templates' })).toBeVisible();
  await expect(page.getByText('Ship it cat', { exact: true })).toBeVisible();
});

test('mobile admin navigation remains usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await gotoAdmin(page, '/admin');

  const navigation = page.getByRole('navigation', { name: 'Admin navigation' });
  await expect(navigation).toBeVisible();
  await expect(navigation.getByRole('link', { name: 'Overview' })).toBeVisible();
  await navigation.getByRole('link', { name: 'Templates' }).scrollIntoViewIfNeeded();
  await expect(navigation.getByRole('link', { name: 'Templates' })).toBeVisible();

  await navigation.getByRole('link', { name: 'Sources' }).click();
  await expect(page).toHaveURL(/\/admin\/sources$/);
  await waitForAdminHydration(page);
  await expect(page.getByRole('heading', { name: 'Sources', level: 1 })).toBeVisible();
});

async function signInAsAdmin(page: Page, baseURL: string | undefined, testInfo: TestInfo): Promise<void> {
  await page.context().addCookies([
    {
      name: 'memexpert_access_token',
      value: adminSessionToken(testInfo),
      url: baseURL ?? 'http://127.0.0.1:4174',
      httpOnly: true,
      sameSite: 'Lax'
    }
  ]);
}

function adminSessionToken(testInfo: TestInfo): string {
  const identity = JSON.stringify([
    testInfo.project.name,
    testInfo.workerIndex,
    testInfo.parallelIndex,
    testInfo.repeatEachIndex,
    testInfo.retry,
    testInfo.testId,
    testInfo.file,
    testInfo.titlePath
  ]);
  return `smoke-admin-${createHash('sha256').update(identity).digest('hex').slice(0, 24)}`;
}

async function gotoAdmin(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await waitForAdminHydration(page);
}

async function waitForAdminHydration(page: Page): Promise<void> {
  const shell = page.locator('[data-admin-shell]');
  await expect(shell).toHaveCount(1);
  await expect(shell).toHaveAttribute('data-admin-hydrated', 'true');
}

async function openDisclosure(scope: Locator, title: string): Promise<Locator> {
  const details = scope.locator(`details[data-advanced-section="${title}"]`);
  await expect(details).toHaveCount(1);
  await details.locator(':scope > summary').click();
  await expect(details).toHaveJSProperty('open', true);
  return details;
}
