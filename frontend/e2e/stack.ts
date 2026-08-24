/**
 * Where the local stack lives, and how a test observes it from outside.
 *
 * Every path and port the browser tests need is derived here, once, because
 * `playwright.config.ts` is re-imported by each worker process: anything
 * computed at import time has to come out the same in all of them, so a
 * randomly-chosen port would give the workers a different stack than the one
 * global setup started. Ports are therefore fixed defaults with environment
 * overrides, not allocated.
 *
 * The defaults deliberately avoid 8000. `frontend/vite.config.ts` proxies to
 * 127.0.0.1:8000, so a developer running `pnpm dev` against a hand-started
 * stack keeps working while this suite runs beside it.
 *
 * ## Why the server's access log is an assertion target
 *
 * The SPA opens its `EventSource` inside a `SharedWorker`. Playwright cannot
 * see that traffic: `page.on("request")` reports requests made by the page
 * and its dedicated workers, and a SharedWorker is neither — it is a separate
 * browser target with no page association. This was checked, not assumed; a
 * `page.on("request")` filter for the stream path never fires while the run
 * it is watching streams into the DOM.
 *
 * uvicorn logs a request line when the response *starts*, not when it ends
 * (also checked: the line for a 10-second SSE stream appears within a second
 * of the connection opening). So counting
 * `GET /api/runs/{id}/stream` lines in the stack log is a truthful count of
 * how many SSE connections the browser opened for a run, taken from the only
 * side of the wire that can see them.
 *
 * The counterpart risk is honest to state: the count includes *reconnects*.
 * A test that asserts "still one connection" is asserting that nothing cut
 * the stream during a window of a few seconds on loopback, which is the
 * environment these run in. Against a real ingress that cuts long
 * connections, a rising count would be correct behaviour, not a regression.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

export const FRONTEND_DIR = path.resolve(HERE, "..");
export const REPO_ROOT = path.resolve(HERE, "../..");

export const APP_PORT = Number(process.env.DBX_E2E_APP_PORT ?? 8811);
export const LAUNCHER_PORT = Number(process.env.DBX_E2E_LAUNCHER_PORT ?? APP_PORT + 1);
export const BASE_URL = `http://127.0.0.1:${APP_PORT}`;

/**
 * Nothing this suite produces goes inside the repository — not the build it
 * serves, not the Postgres cluster, not Playwright's own traces. `frontend/`
 * ignores `dist`, but it ignores nothing else, and a browser test that leaves
 * untracked files behind in a tree four agents are working in is a bad
 * neighbour.
 */
export const WORK_DIR =
  process.env.DBX_E2E_WORK_DIR ?? path.join(os.tmpdir(), "dbx-leaning-e2e");
export const DIST_DIR = path.join(WORK_DIR, "dist");
export const STATE_DIR = path.join(WORK_DIR, "state");
export const STACK_LOG = path.join(WORK_DIR, "dev-stack.log");
export const PID_FILE = path.join(WORK_DIR, "dev-stack.pid");
export const ARTIFACT_DIR = path.join(WORK_DIR, "artifacts");

/** The app's ceiling, which the 429 test drives up against. Free Edition's
 *  real account-wide limit is 5 and `dev_stack.py` defaults to it. */
export const MAX_CONCURRENT_RUNS = 5;

/** How many SSE connections the app has served for this run since the stack
 *  started. See the header for why this is read from the log. */
export function streamConnectionCount(runId: string): number {
  const log = fs.existsSync(STACK_LOG) ? fs.readFileSync(STACK_LOG, "utf8") : "";
  const pattern = new RegExp(`GET /api/runs/${runId}/stream`, "g");
  return (log.match(pattern) ?? []).length;
}

export interface DurableLogRow {
  run_id: string;
  seq: number;
  message: string;
  client_visible: boolean;
}

/**
 * The run's log lines as the durable writer actually persisted them.
 *
 * This is the local stand-in for Unity Catalog — `DBX_WRITER=jsonl`, one file
 * per table under the state directory. It is the independent oracle for the
 * live path: the durable writer runs in parallel with it and never drops, so
 * whatever the browser rendered has to be a subset of this.
 *
 * Only meaningful once the run is terminal; the writer flushes on size, on a
 * 30-second age bound, and at end of run.
 */
export function durableLogs(runId: string): DurableLogRow[] {
  const file = path.join(STATE_DIR, "delta", "main.dbx_leaning.run_logs.jsonl");
  if (!fs.existsSync(file)) return [];
  return fs
    .readFileSync(file, "utf8")
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as DurableLogRow)
    .filter((row) => row.run_id === runId);
}
