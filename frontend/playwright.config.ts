/**
 * Browser tests, against the real stack.
 *
 * `e2e/global-setup.ts` builds the SPA and starts `scripts/dev_stack.py` —
 * the real app, the real job harness, real Postgres, real SSE. If any of that
 * cannot start, setup throws with the stack's own output; nothing here mocks
 * a server or skips a test to keep the run green.
 */

import { defineConfig } from "@playwright/test";

import { ARTIFACT_DIR, BASE_URL } from "./e2e/stack";

export default defineConfig({
  testDir: "./e2e",

  /*
   * `.e2e.ts`, NOT `.spec.ts` or `.test.ts`. Vitest's default `include`
   * covers `**\/*.{test,spec}.?(c|m)[jt]s?(x)` from the project root, and
   * `vite.config.ts` sets no `include` of its own — so a Playwright file
   * named `*.spec.ts` anywhere under `frontend/` would be collected by
   * `pnpm test` and fail in jsdom with no browser behind it. The suffix is
   * what keeps the two runners apart.
   */
  testMatch: "**/*.e2e.ts",

  /** Traces, screenshots and other per-test artifacts, out of the repo — see
   *  `e2e/stack.ts` on why nothing is written inside the tree. */
  outputDir: ARTIFACT_DIR,

  /*
   * One worker, no parallelism, and it is not a performance compromise:
   * the account-wide ceiling of five concurrent runs is global state shared by
   * every test, and so is the app's access log that the connection-count
   * assertions read. Two files running at once would make both meaningless.
   */
  fullyParallel: false,
  workers: 1,

  /*
   * No retries, on purpose. These tests exist to catch the things that only
   * break against a real browser and a real transport, and those failures are
   * frequently intermittent — a retry would hide exactly the class of bug the
   * suite is for. A flake here is a finding, not noise to be smoothed over.
   */
  retries: 0,

  // Model runs take tens of seconds by design; see WATCHABLE_ITERATIONS.
  timeout: 180_000,
  expect: { timeout: 20_000 },
  globalTimeout: 20 * 60_000,

  globalSetup: "./e2e/global-setup.ts",
  reporter: [["list"]],

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },

  // Chromium only. The transport leans on SharedWorker and IndexedDB, which
  // this suite asserts through Chromium's implementation; adding WebKit or
  // Firefox would need their browser binaries downloaded, and this
  // environment has Chromium preinstalled and no network for the rest.
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
