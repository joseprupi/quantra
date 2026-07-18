import { defineConfig, devices } from '@playwright/test'

/**
 * Two projects:
 *
 *  - `chromium` (the original smoke set): hermetic specs in e2e/*.spec.ts that
 *    run against the Vite dev server with all backend/Firebase traffic
 *    route-intercepted. `npm run test:e2e`.
 *
 *  - `full`: the full-coverage journey suite in e2e/full/**. Real browser,
 *    real clicks, REAL backend — point it at a running self-hosted stack
 *    (root `docker-compose.yml`) via E2E_PORTAL_URL (default
 *    http://localhost:5173, the compose portal port). No route interception,
 *    no mocks; every journey drives the portal UI and cross-checks pricing
 *    NPVs against the orchestrator API directly. `npm run test:e2e:full`.
 */

const FULL_STACK_URL = process.env.E2E_PORTAL_URL || 'http://localhost:5173'

// Dev-server port for the hermetic smoke set — overridable so the suite can
// run isolated when something else (another checkout's dev server) already
// occupies 5173 and would otherwise be silently reused.
const DEV_PORT = process.env.E2E_DEV_PORT || '5173'
const DEV_URL = `http://localhost:${DEV_PORT}`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  reporter: 'list',
  use: {
    baseURL: DEV_URL,
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      testIgnore: /full\//,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'full',
      testMatch: /full\/.*\.spec\.ts/,
      retries: 1,
      timeout: 120_000,
      use: {
        ...devices['Desktop Chrome'],
        baseURL: FULL_STACK_URL,
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
      },
    },
  ],
  // The hermetic smoke set needs the dev server; the full suite brings its
  // own stack (E2E_PORTAL_URL set ⇒ no webServer is started).
  webServer: process.env.E2E_PORTAL_URL
    ? undefined
    : {
        command: `npm run dev -- --port ${DEV_PORT} --strictPort`,
        url: DEV_URL,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
})
