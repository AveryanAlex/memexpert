import { defineConfig, devices } from '@playwright/test';

const artifactsDir = process.env.E2E_ARTIFACTS_DIR ?? './playwright-e2e-artifacts';
const browserChannel = process.env.PLAYWRIGHT_CHANNEL;

export default defineConfig({
  testDir: './specs',
  timeout: 45_000,
  expect: {
    timeout: 10_000
  },
  fullyParallel: false,
  workers: 1,
  outputDir: `${artifactsDir}/playwright-test-results`,
  reporter: [
    ['list'],
    ['html', { outputFolder: `${artifactsDir}/playwright-report`, open: 'never' }],
    ['json', { outputFile: `${artifactsDir}/playwright-report/results.json` }],
    ['junit', { outputFile: `${artifactsDir}/playwright-report/junit.xml` }]
  ],
  use: {
    baseURL: process.env.E2E_FRONTEND_BASE_URL ?? 'http://frontend:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure'
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        ...(browserChannel ? { channel: browserChannel } : {})
      }
    }
  ]
});
