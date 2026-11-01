import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.e2e.ts',
  use: {
    baseURL: 'http://127.0.0.1:28641',
    headless: true,
    screenshot: 'only-on-failure',
    launchOptions: process.platform === 'win32' ? { executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' } : {},
  },
  webServer: { command: 'npm run dev -- --host 127.0.0.1 --port 28641 --strictPort', url: 'http://127.0.0.1:28641', reuseExistingServer: true },
});
