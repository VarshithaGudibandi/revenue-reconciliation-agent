import { defineConfig } from "@playwright/test";

// The tests run against a real server in demo mode (see the README), one browser, in order:
// loading the corrected export changes the data the later tests see.
export default defineConfig({
  testDir: "e2e",
  testMatch: "*.e2e.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8000",
    launchOptions: {
      executablePath: process.env.E2E_CHROMIUM || undefined,
      args: ["--no-sandbox"],
    },
  },
});
