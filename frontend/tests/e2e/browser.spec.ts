import { expect, test } from '@playwright/test';
import { readSeedArtifact, seededByCategory } from './seed';

test('public search UI opens detail and renders media through imgproxy', async ({ page }) => {
  const seed = readSeedArtifact();
  const cat = seededByCategory(seed, 'cat');

  await page.goto('/');

  await page.getByLabel('Search memes').fill(cat.query);
  await page.getByRole('button', { name: 'Search' }).click();

  await expect(page).toHaveURL(/\/search\?q=cat/);
  await expect(page.getByText('Results for “cat”')).toBeVisible();

  const result = page.getByRole('link', { name: 'Open Deterministic cat smoke meme' }).first();
  await expect(result).toBeVisible();
  await result.click();

  await expect(page).toHaveURL(new RegExp(`/memes/${cat.slug}$`));
  await expect(page.getByRole('heading', { name: 'Deterministic cat smoke meme' })).toBeVisible();

  const media = page.getByRole('img', { name: 'Deterministic cat smoke meme' }).first();
  await expect(media).toBeVisible();
  await expect(media).toHaveAttribute('src', /http:\/\/imgproxy:8080\/unsafe\//);
  await expect.poll(() => media.evaluate((img) => (img as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);

  const like = page.getByRole('button', { name: /^Like \(/ });
  const save = page.getByRole('button', { name: 'Save', exact: true });
  const pin = page.getByRole('button', { name: 'Pin' });
  await expect(like).toBeVisible();
  await expect(save).toBeVisible();
  await expect(pin).toBeVisible();

  await like.click();
  await expect(page.getByRole('button', { name: /^Unlike \(/ })).toBeVisible();
  await expect(page.getByText('Liked.')).toBeVisible();
});
