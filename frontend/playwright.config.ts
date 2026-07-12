import { createHash, randomUUID } from 'node:crypto';
import { defineConfig, devices } from '@playwright/test';

const browserChannel = process.env.PLAYWRIGHT_CHANNEL;
const reuseExistingServer = process.env.PLAYWRIGHT_REUSE_EXISTING_SERVER === '1';
const explicitRunId = environmentValue('SMOKE_RUN_ID');
const runId = explicitRunId ?? randomUUID();
// Playwright reloads this config in worker processes; propagate the generated identity to keep one invocation coherent.
if (!explicitRunId) process.env.SMOKE_RUN_ID = runId;
const runHash = createHash('sha256').update(runId).digest('hex');
const portPair = derivedPortPair(runHash);
const mockApiPort = environmentPort('SMOKE_API_PORT') ?? portPair.mockApiPort;
const webPort = environmentPort('SMOKE_WEB_PORT') ?? portPair.webPort;
const outputRunId = `${runId.replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 40) || 'run'}-${runHash.slice(0, 12)}`;

if (mockApiPort === webPort) {
  throw new Error('SMOKE_API_PORT and SMOKE_WEB_PORT must use different non-privileged ports.');
}

if (reuseExistingServer && (!explicitRunId || !environmentValue('SMOKE_API_PORT') || !environmentValue('SMOKE_WEB_PORT'))) {
  throw new Error('PLAYWRIGHT_REUSE_EXISTING_SERVER=1 requires explicit SMOKE_RUN_ID, SMOKE_API_PORT, and SMOKE_WEB_PORT values.');
}

export default defineConfig({
  testDir: './tests/smoke',
  outputDir: `./test-results/smoke/${outputRunId}`,
  metadata: {
    smokeRunId: runId,
    mockApiPort,
    webPort
  },
  timeout: 30_000,
  retries: 0,
  expect: {
    timeout: 5_000
  },
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure'
  },
  webServer: [
    {
      command: 'node tests/smoke/mock-api.mjs',
      env: {
        PORT: String(mockApiPort),
        SMOKE_RUN_ID: runId
      },
      reuseExistingServer,
      url: `http://127.0.0.1:${mockApiPort}/health`
    },
    {
      command: `pnpm exec vite dev --host 127.0.0.1 --port ${webPort}`,
      env: {
        API_BASE_URL: `http://127.0.0.1:${mockApiPort}`,
        SMOKE_RUN_ID: runId
      },
      reuseExistingServer,
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

function environmentValue(name: string): string | null {
  const value = process.env[name]?.trim();
  return value ? value : null;
}

function environmentPort(name: string): number | null {
  const value = environmentValue(name);
  if (value === null) return null;

  const port = Number(value);
  if (!Number.isInteger(port) || port < 1_024 || port > 65_535) {
    throw new Error(`${name} must be an integer between 1024 and 65535.`);
  }
  return port;
}

function derivedPortPair(hash: string): { mockApiPort: number; webPort: number } {
  const firstPort = 20_000;
  const pairCount = 20_000;
  const pairIndex = Number.parseInt(hash.slice(0, 12), 16) % pairCount;
  const mockApiPort = firstPort + pairIndex * 2;
  return { mockApiPort, webPort: mockApiPort + 1 };
}
