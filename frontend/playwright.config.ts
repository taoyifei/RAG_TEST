import { defineConfig, devices } from "@playwright/test";

const browserChannel = process.env.P10_BROWSER_CHANNEL as "chrome" | undefined;

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 12_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.P10_BASE_URL ?? "http://127.0.0.1:8091",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium-desktop",
      use: { ...devices["Desktop Chrome"], channel: browserChannel },
    },
    {
      name: "chromium-mobile",
      use: {
        ...devices["Desktop Chrome"],
        channel: browserChannel,
        viewport: { width: 375, height: 812 },
      },
    },
  ],
  webServer: process.env.P10_EXTERNAL_SERVER
    ? undefined
    : {
        command:
          "python ../scripts/serve_p10.py --port 8091 --frontend-dir dist",
        cwd: ".",
        url: "http://127.0.0.1:8091/ready",
        reuseExistingServer: false,
        timeout: 60_000,
      },
});
