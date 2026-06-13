import { test } from '../fixtures/app';
import { seededByCategory } from '../helpers/seed';

test('guest can favorite and unfavorite but cannot use full-account library actions', async ({ app, seed }) => {
  const cat = seededByCategory(seed, 'cat');

  await app.detail.goto(cat.slug);
  await app.detail.expectOpen(cat);
  await app.detail.likeAndUnlike();
  await app.detail.expectPinFullAccountOnly();

  await app.home.goto();
  await app.home.expectGuestCollectionCreationUnavailable();
});
