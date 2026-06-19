import { test } from '../fixtures/app';
import { loginViaEmail, removeCollectionMemberViaApi } from '../helpers/api';
import { collectionManagementFixture } from '../helpers/seed';
import { CollectionPage } from '../pages/CollectionPage';
import { SearchPage } from '../pages/SearchPage';

test('full accounts can join, manage roles, search a collection, and reorder pins', async ({
  apiBaseUrl,
  app,
  browser,
  page,
  seed
}) => {
  const fixture = collectionManagementFixture(seed);
  const collectionId = fixture.collection.id;
  const collectionMeme = fixture.saved_memes[0];

  await loginViaEmail(page, apiBaseUrl, fixture.owner);
  await removeCollectionMemberViaApi(page, apiBaseUrl, {
    collectionId,
    memberUserId: fixture.member.user_id
  });

  await app.collection.goto(collectionId);
  await app.collection.expectOwnerControls(fixture);
  await app.collection.expectSavedMemeVisible(collectionMeme);

  const memberContext = await browser.newContext({
    baseURL: process.env.E2E_FRONTEND_BASE_URL ?? 'http://frontend:3000'
  });
  const memberPage = await memberContext.newPage();
  const memberCollection = new CollectionPage(memberPage);
  const memberSearch = new SearchPage(memberPage);

  try {
    await loginViaEmail(memberPage, apiBaseUrl, fixture.member);
    await memberCollection.joinInvite(fixture.invite.join_path, collectionId);
    await memberCollection.expectViewerGuidance(fixture);

    await app.collection.goto(collectionId);
    await app.collection.updateMemberRole(fixture.member.user_id, 'editor');

    await memberCollection.goto(collectionId);
    await memberCollection.expectEditorControls(fixture);

    await memberSearch.searchCollections({
      query: collectionMeme.query,
      collectionTitles: ['Favorites', fixture.collection.title]
    });
    await memberSearch.expectCollectionScopeUrl({
      query: collectionMeme.query,
      requiredCollectionId: collectionId,
      minimumCollectionIds: 2
    });
    await memberSearch.expectResultVisible(collectionMeme);
  } finally {
    await memberContext.close();
  }

  await app.profile.moveFirstPinDownAndExpectSaved();
});
