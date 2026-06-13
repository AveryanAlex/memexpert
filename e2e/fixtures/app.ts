import { test as base } from '@playwright/test';
import { E2EApi } from '../helpers/api';
import { readSeedArtifact, type SeedArtifact } from '../helpers/seed';
import { HomePage } from '../pages/HomePage';
import { MemeDetailPage } from '../pages/MemeDetailPage';
import { ProfilePage } from '../pages/ProfilePage';
import { SearchPage } from '../pages/SearchPage';

type AppFixtures = {
  apiBaseUrl: string;
  operatorToken: string;
  seed: SeedArtifact;
  api: E2EApi;
  app: {
    home: HomePage;
    search: SearchPage;
    detail: MemeDetailPage;
    profile: ProfilePage;
  };
};

export const test = base.extend<AppFixtures>({
  apiBaseUrl: [process.env.E2E_API_BASE_URL ?? 'http://api:8000', { option: true }],
  operatorToken: [process.env.E2E_OPERATOR_TOKEN ?? 'memexpert-e2e-pipeline-operator-token-min-32', { option: true }],
  seed: async ({}, use) => {
    await use(readSeedArtifact());
  },
  api: async ({ request, apiBaseUrl, operatorToken }, use) => {
    await use(new E2EApi(request, apiBaseUrl, operatorToken));
  },
  app: async ({ page }, use) => {
    await use({
      home: new HomePage(page),
      search: new SearchPage(page),
      detail: new MemeDetailPage(page),
      profile: new ProfilePage(page)
    });
  }
});

export { expect } from '@playwright/test';
