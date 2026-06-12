import { defineConfig, devices } from '@playwright/test';

const mockApiPort = Number(process.env.SMOKE_API_PORT ?? 8787);
const webPort = Number(process.env.SMOKE_WEB_PORT ?? 4174);
const browserChannel = process.env.PLAYWRIGHT_CHANNEL;

export default defineConfig({
  testDir: './tests/smoke',
  timeout: 30_000,
  expect: {
    timeout: 5_000
  },
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: 'on-first-retry'
  },
  webServer: [
    {
      command: 'node tests/smoke/mock-api.mjs',
      env: {
        PORT: String(mockApiPort)
      },
      reuseExistingServer: !process.env.CI,
      url: `http://127.0.0.1:${mockApiPort}/health`
    },
    {
      command: `pnpm exec vite dev --host 127.0.0.1 --port ${webPort}`,
      env: {
        API_BASE_URL: `http://127.0.0.1:${mockApiPort}`
      },
      reuseExistingServer: !process.env.CI,
      url: `http://127.0.0.1:${webPort}`
    }
  ],
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
