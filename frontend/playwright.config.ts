import { defineConfig, devices } from "@playwright/test";

const browserChannel = process.env.P10_BROWSER_CHANNEL as "chrome" | undefined;
const externalServer = process.env.P10_EXTERNAL_SERVER;
const externalBaseUrl = process.env.P10_BASE_URL;
const desktopBaseUrl =
  process.env.P10_DESKTOP_BASE_URL ??
  externalBaseUrl ??
  "http://127.0.0.1:8091";
const mobileBaseUrl =
  process.env.P10_MOBILE_BASE_URL ??
  externalBaseUrl ??
  "http://127.0.0.1:8092";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 12_000 },
  fullyParallel: false,
  // 单个 Product 进程按 loopback 主体执行真实限流；串行运行避免测试项目
  // 彼此消耗同一安全桶，同时仍完整覆盖桌面与移动视口。
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium-desktop",
      use: {
        ...devices["Desktop Chrome"],
        channel: browserChannel,
        baseURL: desktopBaseUrl,
      },
    },
    {
      name: "chromium-mobile",
      use: {
        ...devices["Desktop Chrome"],
        channel: browserChannel,
        baseURL: mobileBaseUrl,
        viewport: { width: 375, height: 812 },
      },
    },
  ],
  webServer: externalServer
    ? undefined
    : [8091, 8092].map((port) => ({
        // 两个视口项目使用独立临时 Product Runtime，避免跨项目共享
        // 生产等价限流桶和数据状态；每个项目内部仍串行执行真实门禁。
        command: `../.venv/bin/python ../scripts/serve_p10.py --port ${port} --frontend-dir dist`,
        cwd: ".",
        url: `http://127.0.0.1:${port}/ready`,
        reuseExistingServer: false,
        timeout: 60_000,
      })),
});
