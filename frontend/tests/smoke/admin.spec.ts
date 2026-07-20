import { createHash } from 'node:crypto';
import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test';

const adminFixture = {
  memeId: '1cb7b083-dc9f-45a6-9e4c-3dc497651a04',
  mediaFileId: '1cb7b083-dc9f-45a6-9e4c-3dc497651a05',
  readyAccountId: '1cb7b083-dc9f-45a6-9e4c-3dc497651a06',
  healthySourceId: '1cb7b083-dc9f-45a6-9e4c-3dc497651a08',
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

test('admin analytics keeps a shared UTC range across dashboards and exposes query drill-down', async ({ page }) => {
  await gotoAdmin(page, '/admin/analytics?start_date=2026-06-01&end_date=2026-06-30');

  const navigation = page.getByRole('navigation', { name: 'Admin navigation' });
  await expect(navigation.getByRole('link', { name: 'Analytics' })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('heading', { name: 'Analytics overview' })).toBeVisible();
  await expect(page.getByLabel('Start date')).toHaveValue('2026-06-01');
  await expect(page.getByLabel('End date')).toHaveValue('2026-06-30');
  await expect(page.getByRole('heading', { name: 'From search to saved media' })).toBeVisible();

  await page.getByRole('navigation', { name: 'Analytics sections' }).getByRole('link', { name: 'Engagement' }).click();
  await expect(page).toHaveURL(/\/admin\/analytics\/engagement\?start_date=2026-06-01&end_date=2026-06-30$/);
  await expect(page.getByRole('heading', { name: 'Search query explorer' })).toBeVisible();
  await page.getByRole('link', { name: 'Niche' }).click();
  await expect(page).toHaveURL(/sort=niche/);
  await expect(page.getByText('frog reaction', { exact: true }).first()).toBeVisible();
  await page.getByRole('link', { name: 'View outcomes' }).first().click();
  await expect(page).toHaveURL(/query_key=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef/);
  await expect(page).not.toHaveURL(/frog(?:%20|\+| )reaction/);
  await expect(page.getByRole('heading', { name: 'frog reaction' })).toBeVisible();
  await expect(page.getByText(adminFixture.memeId, { exact: true })).toBeVisible();
});

test('recovery selection follows the batch action and can select all compatible rows on the page', async ({ page }) => {
  await gotoAdmin(page, '/admin/recovery');

  await expect(page.getByRole('heading', { name: 'Replay & Repair' })).toBeVisible();
  const action = page.getByRole('combobox', { name: 'Batch action' });
  await expect(action).toHaveValue('resume_backfill');

  const selectAll = page.getByLabel('Select all compatible recovery work on this page');
  const memach = page.getByLabel('Select @memach backfill for Resume backfill');
  const log4inpowerken = page.getByLabel('Select @log4inpowerken backfill for Resume backfill');
  const ocrOneForResume = page.getByLabel('Select OCR file one for Resume backfill');
  const blockedForResume = page.getByLabel('Select Blocked Telegram post for Resume backfill');

  await expect(memach).toBeEnabled();
  await expect(log4inpowerken).toBeEnabled();
  await expect(ocrOneForResume).toBeDisabled();
  await expect(blockedForResume).toBeDisabled();

  await memach.check();
  await expect(memach).toBeChecked();
  await expect(selectAll).not.toBeChecked();
  await expect(selectAll).toHaveJSProperty('indeterminate', true);
  await expect(page.getByText('1 of 2 compatible rows selected for Resume backfill.')).toBeVisible();

  let formPayload = await recoveryFormPayload(page);
  expect(formPayload.capability).toBe('resume_backfill');
  expect(formPayload.items.map((item) => JSON.parse(item))).toEqual([
    { kind: 'backfill', id: 'smoke-backfill-memach', version: 'backfill-version-1' }
  ]);

  await selectAll.click();
  await expect(selectAll).toBeChecked();
  await expect(memach).toBeChecked();
  await expect(log4inpowerken).toBeChecked();
  await expect(ocrOneForResume).not.toBeChecked();
  await expect(page.getByText('2 of 2 compatible rows selected for Resume backfill.')).toBeVisible();

  await action.selectOption('retry_stage');
  await expect(page.getByText('0 of 2 compatible rows selected for Retry stage.')).toBeVisible();
  await expect(selectAll).not.toBeChecked();
  await expect(selectAll).toHaveJSProperty('indeterminate', false);
  await expect(page.getByLabel('Select @memach backfill for Retry stage')).toBeDisabled();
  const ocrOne = page.getByLabel('Select OCR file one for Retry stage');
  const ocrTwo = page.getByLabel('Select OCR file two for Retry stage');
  await expect(ocrOne).toBeEnabled();
  await expect(ocrTwo).toBeEnabled();

  await selectAll.click();
  await expect(ocrOne).toBeChecked();
  await expect(ocrTwo).toBeChecked();
  await expect(page.getByLabel('Select Blocked Telegram post for Retry stage')).toBeDisabled();

  formPayload = await recoveryFormPayload(page);
  expect(formPayload.capability).toBe('retry_stage');
  expect(formPayload.items.map((item) => JSON.parse(item))).toEqual([
    { kind: 'pipeline_stage', id: 'smoke-file-ocr-1:ocr', version: 'ocr-version-1' },
    { kind: 'pipeline_stage', id: 'smoke-file-ocr-2:ocr', version: 'ocr-version-2' }
  ]);

  await action.selectOption('replay_stage');
  const ocrCascade = page.getByLabel('Select OCR file one for Replay stage');
  await expect(ocrCascade).toBeEnabled();
  await ocrCascade.check();
  const previewForm = page.locator('#batch-preview-form');
  const terminalOverride = previewForm.getByLabel(
    'I acknowledge that this terminal failure is being overridden for an audited replay.'
  );
  await expect(terminalOverride).not.toBeVisible();
  await previewForm.getByLabel('Replay scope').selectOption('stage_and_dependents');
  await expect(terminalOverride).toBeVisible();
  await expect(terminalOverride).toHaveAttribute('required', '');
});

test('Replay & Repair materializes the uncapped outdated-video query and exposes job failures first', async ({ page }) => {
  await gotoAdmin(page, '/admin/recovery?view=regenerate');

  await expect(page.getByRole('heading', { name: 'Replay & Repair' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Outdated web videos' })).toBeVisible();
  await expect(page.getByText('7,400', { exact: true })).toBeVisible();
  await expect(page.getByText('web-h264-aac-1080p30-v2', { exact: false })).toBeVisible();
  const regenerate = page.locator('form[action^="?/previewRecoveryBatch"]').filter({
    has: page.getByRole('button', { name: 'Select all matching', exact: true })
  });
  await expect(regenerate.locator('input[name="selector_type"]')).toHaveValue('query');
  await expect(regenerate.locator('input[name="query_filters"]')).toHaveValue('{"outdated_web_video":true}');
  await expect(regenerate.getByLabel('Retry limit')).toHaveValue('3');
  await regenerate.getByLabel('Audit reason').fill('Regenerate every outdated smoke derivative.');
  await regenerate.getByLabel(/terminal-failed Transcode roots/).check();
  await regenerate.getByRole('button', { name: 'Select all matching', exact: true }).click();
  await expect(page.getByRole('status').first()).toContainText('Exact preview preparation started');
  await expect(page.getByText('1,200 scanned · 1,000 matched · 2 excluded')).toBeVisible();

  await page.getByRole('navigation', { name: 'Replay and Repair sections' }).getByRole('link', { name: /Jobs/ }).click();
  await expect(page.getByRole('heading', { name: 'Jobs', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Replay stage' }).click();
  await expect(page).toHaveURL(new RegExp(`/admin/recovery/batches/77777777-7777-4777-8777-777777777777$`));
  await expect(page.getByRole('heading', { name: 'Replay stage' })).toBeVisible();
  await expect(page.getByText('Smoke requester')).toBeVisible();
  await expect(page.getByText('Changed Since Snapshot')).toBeVisible();
  const items = page.getByRole('table');
  await expect(items.getByRole('row').nth(1)).toContainText('Failed');
  await expect(items.getByText('OCR exceeded its safe processing deadline.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Preview retry of failed items' })).toBeVisible();
});

test('admin adds a public Telegram source through the selected ready account and pauses that source', async ({ page }) => {
  await gotoAdmin(page, '/admin/sources');

  await expect(page.getByRole('heading', { name: 'Sources', level: 1 })).toBeVisible();
  const sourceTable = page.getByRole('table', {
    name: 'Configured sources with health, activity, catalog counts, and operator actions.'
  });
  const healthHeader = sourceTable.getByRole('columnheader', { name: /Health \/ account/ });
  await expect(healthHeader).toHaveAttribute('aria-sort', 'ascending');
  const sourceHeader = sourceTable.getByRole('columnheader', { name: /Source/ });
  await sourceHeader.getByRole('button').click();
  await expect(sourceHeader).toHaveAttribute('aria-sort', 'ascending');
  const rowHeaders = sourceTable.getByRole('rowheader');
  await expect(rowHeaders.nth(0)).toContainText('Daily cats');
  await expect(rowHeaders.nth(1)).toContainText('Retro memes');
  await expect(rowHeaders.nth(2)).toContainText('Small memes');
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
  const addSourceButton = quickAddForm.getByRole('button', { name: 'Add source', exact: true });
  await addSourceButton.click();
  await expect(quickAddForm.getByText('Telegram source added and ready to fetch.', { exact: true })).toBeVisible();
  const transientRefreshError = page.getByText('Source catalog is restarting after creation.', { exact: true });
  await expect(transientRefreshError).toBeVisible();
  await expect(addSourceButton).toBeEnabled();
  await expect(accountSelect).toHaveValue(adminFixture.readyAccountId);
  await expect(telegramSuggestion).toBeVisible();
  await expect(transientRefreshError).not.toBeVisible();

  const addedSource = sourceTable.getByRole('row').filter({ hasText: 'Fresh Public Channel' });
  await expect(addedSource.getByText('@fresh_public_channel · Telegram', { exact: true })).toBeVisible();
  await expect(addedSource.getByText(/Account: Meme desk account/)).toBeVisible();

  await addedSource.getByRole('button', { name: 'Pause Fresh Public Channel' }).click();
  await expect(page.getByRole('status')).toContainText('Source paused.');
  const pausedSource = sourceTable.getByRole('row').filter({ hasText: 'Fresh Public Channel' });
  await expect(pausedSource.getByText('Paused', { exact: true })).toBeVisible();

  await pausedSource.getByRole('link', { name: 'Manage Fresh Public Channel' }).click();
  await expect(page).toHaveURL(new RegExp(`/admin/sources/${adminFixture.quickAddedSourceId}$`));
  await expect(page.getByRole('heading', { name: 'Fresh Public Channel', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Source management', level: 2 })).toBeVisible();
  await expect(page.locator('details[data-advanced-section="Diagnostics"]')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeVisible();
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

test('admin inspects source message indexing and queues an older-history pass', async ({ page }) => {
  await gotoAdmin(page, `/admin/sources/${adminFixture.healthySourceId}`);

  await expect(page.getByRole('heading', { name: 'Daily cats', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Indexing summary' })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'Materialized' })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'Telegram context' })).toBeVisible();
  await expect(page.getByText('Metadata captured', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Metadata missing', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Partially indexed', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Embedding provider unavailable.')).toBeVisible();

  const capturedPost = page.getByRole('row').filter({ has: page.getByRole('link', { name: '#184', exact: true }) });
  await expect(capturedPost.getByText('Cats & coffee')).toBeVisible();
  await expect(capturedPost.getByText('9007199254740993', { exact: true })).toBeVisible();
  await expect(capturedPost.getByText('Reply to:', { exact: true })).toBeVisible();
  await expect(capturedPost.getByText('#179', { exact: true })).toBeVisible();
  await expect(capturedPost.getByText('Edited 2026-01-01 00:05 UTC', { exact: true })).toBeVisible();
  await expect(capturedPost.getByText('Not marked deleted', { exact: true })).toBeVisible();

  const deletedPost = page.getByRole('row').filter({ has: page.getByRole('link', { name: '#181', exact: true }) });
  await expect(deletedPost.getByText('Deleted from Telegram', { exact: true })).toBeVisible();
  await expect(deletedPost.getByText('This retained caption remains available after deletion.', { exact: true })).toBeVisible();
  await expect(deletedPost.getByText('Deletion observed 2026-01-01 00:15 UTC', { exact: true })).toBeVisible();

  const textOnlyPost = page.getByRole('row').filter({ has: page.getByRole('link', { name: '#180', exact: true }) });
  await expect(textOnlyPost.getByText('Standalone text-only Telegram post.', { exact: true })).toBeVisible();

  const backfillForm = page.locator('form[action="?/backfillSourceChannel"]');
  const messageLimit = backfillForm.getByLabel('Older messages to fetch');
  await expect(messageLimit).toHaveValue('5000');
  await backfillForm.getByRole('button', { name: 'Fetch older messages' }).click();
  await expect(page.getByRole('status').first()).toContainText('Older-message backfill queued for 5,000 messages.');
  await expect(page.getByText('Backfill status: Queued')).toBeVisible();
  await expect(messageLimit).toBeDisabled();
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
  await expect(page.getByRole('heading', { name: 'Processing' })).toBeVisible();
  await expect(page.getByText('web-h264-aac-1080p30-v2')).toBeVisible();
  await expect(page.getByText('Audio present').first()).toBeVisible();
  const processingFile = page.locator(`[data-processing-file="${adminFixture.mediaFileId}"]`);
  const stageActions = processingFile.locator('details[data-recovery-action-menu]').nth(1);
  await stageActions.locator(':scope > summary').click();
  await stageActions.getByRole('button', { name: 'Replay stage' }).click();
  const replayDialog = page.getByRole('dialog');
  await expect(replayDialog.getByText('ocr → embed → classify → sync_qdrant → sync_meili')).toBeVisible();
  await expect(replayDialog.getByLabel('Replay scope')).toHaveValue('stage_only');
  await expect(replayDialog.getByLabel('Retry limit')).toHaveValue('3');
  await expect(replayDialog.getByText('Stage-only replay leaves existing dependents untouched.')).toBeVisible();
  await expect(replayDialog.getByLabel('I acknowledge the terminal override.')).not.toBeVisible();
  await expect(replayDialog.getByText(/External provider output or semantic merge results/)).not.toBeVisible();
  await replayDialog.getByLabel('Replay scope').selectOption('stage_and_dependents');
  await expect(replayDialog.getByText('Search targets may run concurrently after classification.')).toBeVisible();
  await expect(replayDialog.getByLabel('I acknowledge the terminal override.')).toBeVisible();
  await expect(replayDialog.getByText(/External provider output or semantic merge results/)).toBeVisible();
  await replayDialog.getByRole('button', { name: 'Cancel' }).click();
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

  const sourceTable = page.getByRole('table', {
    name: 'Configured sources with health, activity, catalog counts, and operator actions.'
  });
  const firstSourceRow = sourceTable.getByRole('row').nth(1);
  const edgeCellPositions = await firstSourceRow.locator('th, td').evaluateAll((cells) => [
    getComputedStyle(cells[0]).position,
    getComputedStyle(cells[cells.length - 1]).position
  ]);
  expect(edgeCellPositions).toEqual(['static', 'static']);

  const postsHeader = sourceTable.getByRole('columnheader', { name: /Posts/ });
  await postsHeader.scrollIntoViewIfNeeded();
  await expect(postsHeader).toBeVisible();
  await postsHeader.getByRole('button').click();
  await expect(postsHeader).toHaveAttribute('aria-sort', 'descending');
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

async function recoveryFormPayload(page: Page): Promise<{ capability: string; items: string[] }> {
  return page.locator('#batch-preview-form').evaluate((form) => {
    const data = new FormData(form as HTMLFormElement);
    return {
      capability: String(data.get('capability')),
      items: data.getAll('item').map((item) => String(item))
    };
  });
}
