/**
 * Bring up the real thing, or fail loudly.
 *
 * There is no mock server here and there must never be one. `dev_stack.py`
 * runs the shipped `app/` under uvicorn, the shipped `job/` harness in its
 * own OS process per run, a real embedded Postgres behind the real
 * `PostgresRunStore`, and the real models. The only substituted component is
 * the Databricks Jobs API (`scripts/dev_launcher.py`), which answers
 * `run-now` by spawning a subprocess. Read both docstrings before changing
 * anything in here.
 *
 * Two consequences of the local shape that the tests are written around:
 *
 *  - **There is no SQL warehouse**, so `GET /api/runs/{id}/messages` (the
 *    backfill) and `GET /api/runs/{id}/results` answer 503 and startup
 *    reconciliation is skipped. That makes the live path the *only* way
 *    telemetry can reach the browser, which is what gives the streaming
 *    assertions their teeth.
 *  - **The durable writer is JSONL under the state directory**, not Unity
 *    Catalog. Same messages, same seq numbers, readable from a test — see
 *    `durableLogs` in `stack.ts`.
 *
 * The SPA is served by the app itself from a build in the work directory, the
 * way `app/server/spa.py` serves it in a deploy — not by `vite dev`. That is one
 * fewer moving part than the dev proxy, it puts `/api`, `/ws` and the bundle
 * on one origin exactly as Databricks Apps does, and it means the port is
 * ours to choose rather than pinned to the 8000 baked into `vite.config.ts`.
 * The cost is a production build (~10s) before the first test.
 */

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";

import {
  APP_PORT,
  BASE_URL,
  DIST_DIR,
  CLIENT_DIR,
  LAUNCHER_PORT,
  MAX_CONCURRENT_RUNS,
  PID_FILE,
  REPO_ROOT,
  STACK_LOG,
  STATE_DIR,
  WORK_DIR,
} from "./stack";

const HEALTH_TIMEOUT_MS = 180_000;

function logTail(lines = 40): string {
  if (!fs.existsSync(STACK_LOG)) return "(the stack wrote no output at all)";
  return fs.readFileSync(STACK_LOG, "utf8").split("\n").slice(-lines).join("\n");
}

/** A stack left behind by a crashed run holds the ports and the state
 *  directory. SIGTERM, because that is what `dev_stack.py` turns into an
 *  orderly shutdown of the launcher, the app, every job and Postgres —
 *  SIGKILL on the parent orphans all four (verified the hard way). */
async function killStaleStack(): Promise<void> {
  if (!fs.existsSync(PID_FILE)) return;
  const pid = Number(fs.readFileSync(PID_FILE, "utf8").trim());
  fs.rmSync(PID_FILE, { force: true });
  if (!Number.isInteger(pid) || pid <= 1) return;
  try {
    process.kill(pid, 0);
  } catch {
    return; // already gone
  }
  console.log(`[e2e] terminating a stack left over from an earlier run (pid ${pid})`);
  await stopStack(pid);
}

async function stopStack(pid: number): Promise<void> {
  try {
    process.kill(pid, "SIGTERM");
  } catch {
    return;
  }
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      process.kill(pid, 0);
    } catch {
      return; // exited
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  console.error(`[e2e] stack pid ${pid} ignored SIGTERM for 30s; sending SIGKILL`);
  try {
    process.kill(pid, "SIGKILL");
  } catch {
    /* nothing left to kill */
  }
}

function buildSpa(): void {
  if (process.env.DBX_E2E_SKIP_BUILD === "1" && fs.existsSync(`${DIST_DIR}/index.html`)) {
    console.log(`[e2e] reusing the existing build in ${DIST_DIR}`);
    return;
  }
  console.log(`[e2e] building the SPA into ${DIST_DIR}`);
  // `vite build`, not `bun run build`: `tsc -b` is a separate quality gate
  // with its own command, and four agents are editing src/ — a type error in
  // someone else's in-flight file should not be reported here as "the browser
  // tests cannot start".
  const built = spawnSync(
    "bunx",
    ["vite", "build", "--outDir", DIST_DIR, "--emptyOutDir"],
    { cwd: CLIENT_DIR, encoding: "utf8" },
  );
  if (built.status !== 0) {
    throw new Error(
      `[e2e] the SPA build failed (exit ${built.status}). These tests need a real bundle; ` +
        `there is no stub to fall back to.\n${built.stdout ?? ""}\n${built.stderr ?? ""}`,
    );
  }
}

function startStack(): number {
  const out = fs.openSync(STACK_LOG, "w");
  const child = spawn(
    "uv",
    [
      "run",
      "python",
      "scripts/dev_stack.py",
      "--app-port",
      String(APP_PORT),
      "--launcher-port",
      String(LAUNCHER_PORT),
      "--state-dir",
      STATE_DIR,
      // Every session starts from an empty registry and empty telemetry, so
      // the concurrency test knows what the ceiling is holding and the
      // durable-log oracle only ever sees this session's runs.
      "--reset",
      "--quiet-jobs",
      "--max-concurrent-runs",
      String(MAX_CONCURRENT_RUNS),
    ],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, DBX_FRONTEND_DIST: DIST_DIR },
      stdio: ["ignore", out, out],
    },
  );
  child.unref();
  if (child.pid === undefined) throw new Error("[e2e] could not spawn the dev stack");
  fs.writeFileSync(PID_FILE, String(child.pid));
  return child.pid;
}

async function waitForApp(pid: number): Promise<void> {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  let last = "no attempt made";
  while (Date.now() < deadline) {
    try {
      process.kill(pid, 0);
    } catch {
      throw new Error(
        `[e2e] the dev stack exited before it answered ${BASE_URL}/healthz.\n` +
          `Its output:\n${logTail()}`,
      );
    }
    try {
      const health = await fetch(`${BASE_URL}/healthz`, {
        signal: AbortSignal.timeout(2000),
      });
      if (health.ok) {
        const body = (await health.json()) as { status: string; degraded: Record<string, string> };
        console.log(
          `[e2e] app up at ${BASE_URL} — status ${body.status}, degraded: ` +
            `${Object.keys(body.degraded).join(", ") || "nothing"}`,
        );
        return;
      }
      last = `HTTP ${health.status}`;
    } catch (error) {
      last = String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `[e2e] ${BASE_URL}/healthz did not answer within ${HEALTH_TIMEOUT_MS / 1000}s (${last}).\n` +
      `Stack output:\n${logTail()}`,
  );
}

/** The app answers 503 with `app/server/spa.py`'s NO_BUNDLE message when the dist it
 *  was pointed at has no index.html. Catching that here turns "every test
 *  fails on a blank page" into one sentence. */
async function assertBundleServed(): Promise<void> {
  const page = await fetch(BASE_URL, { signal: AbortSignal.timeout(5000) });
  const body = await page.text();
  if (!page.ok || !body.includes("<div id=\"root\">")) {
    throw new Error(
      `[e2e] ${BASE_URL} did not serve the SPA (HTTP ${page.status}). ` +
        `DBX_FRONTEND_DIST was ${DIST_DIR}.\n${body.slice(0, 400)}`,
    );
  }
}

export default async function globalSetup(): Promise<() => Promise<void>> {
  fs.mkdirSync(WORK_DIR, { recursive: true });
  await killStaleStack();
  buildSpa();
  const pid = startStack();
  await waitForApp(pid);
  await assertBundleServed();

  return async () => {
    console.log("[e2e] stopping the dev stack (app, launcher, jobs, postgres)");
    await stopStack(pid);
    fs.rmSync(PID_FILE, { force: true });
  };
}
