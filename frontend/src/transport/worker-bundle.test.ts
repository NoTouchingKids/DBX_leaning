/**
 * Guards a failure mode that is invisible everywhere else.
 *
 * Vite finds a worker entry by matching `new URL("./worker.ts",
 * import.meta.url)` written INSIDE `new Worker(...)` / `new SharedWorker(...)`.
 * Hoist that expression into a helper and everything still compiles, the dev
 * server still works (it resolves the URL at runtime), and the production
 * build quietly emits no worker chunk — so the deployed app silently falls
 * through to the in-page tier, losing the cross-tab connection sharing that
 * is the entire point on a platform capped at 5 concurrent tasks.
 *
 * This file did exactly that. Hence a test that runs the real build.
 */
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build } from "vite";
import { afterAll, expect, it } from "vitest";

const outDir = mkdtempSync(join(tmpdir(), "dbx-worker-bundle-"));

afterAll(() => rmSync(outDir, { recursive: true, force: true }));

it(
  "emits a separate worker chunk in the production build",
  async () => {
    await build({
      logLevel: "silent",
      build: { outDir, emptyOutDir: true, sourcemap: false },
    });
    const assets = readdirSync(join(outDir, "assets"));
    expect(assets.some((name) => /^worker-.*\.js$/.test(name))).toBe(true);
  },
  60_000,
);
