import { defineConfig, devices } from '@playwright/test';

// docs/test_strategy.md 8: E2E runs against the local Compose profile with deterministic
// policies. The specs arrive in stage 4; `webServer` is wired up with them.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: process.env.WEB_BASE_URL ?? 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
