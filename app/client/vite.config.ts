/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import babel from "@rolldown/plugin-babel";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

/**
 * Where `bun run dev` proxies `/api`, `/ws` and `/healthz`.
 *
 * The default matches `scripts/dev_stack.py::DEFAULT_APP_PORT`, so the
 * documented "start the dev stack, then `bun run dev`" flow needs no environment
 * at all — and dev_stack.py's own comment points back at this file, so the
 * two agree by construction. The override exists because that port is not
 * fixed: `dev_stack.py` takes `--app-port`, and the browser suite runs its own
 * stack on 8811 (`e2e/stack.ts`, deliberately off 8000 so it can run beside a
 * hand-started one). A hardcoded target was the reason `bun run dev` could not
 * be pointed at either.
 *
 * Origin only — scheme, host, port. A path here would be silently ignored by
 * the WebSocket target below, which rebuilds the URL from scratch.
 */
const DEV_API = process.env.DBX_DEV_API ?? "http://127.0.0.1:8000";

/** Same origin as {@link DEV_API}, ws:// or wss:// to match http:// or
 *  https://. Derived rather than configured separately so the two cannot be
 *  pointed at different stacks by setting only one of them. */
const DEV_WS = DEV_API.replace(/^http/, "ws");

/** Shared by `server` and `preview`. `preview` does NOT inherit `server.proxy`
 *  — they are separate option trees — and without its own copy `bun run preview`
 *  served a bundle whose every `/api` call fell through to the SPA fallback
 *  and came back as HTML with a 200, failing later in a JSON parser. */
const apiProxy = {
  "/api": { target: DEV_API, changeOrigin: true },
  "/ws": { target: DEV_WS, ws: true },
  "/healthz": DEV_API,
};

// The app is served by FastAPI, not a Node server — Databricks Apps has no
// Node runtime at deploy time, which is why this is a client-rendered SPA
// (ADR-001). `app/server/spa.py` mounts `dist/assets` at `/assets` and returns
// `index.html` for any other non-API path, so the build must put hashed
// assets under `assets/` and the base must be absolute.
export default defineConfig({
  base: "/",
  plugins: [
    react(),
    // React Compiler, via Babel. plugin-react v6 also has a native Rust path
    // (`compiler: true` + oxc-transform-react) which is faster and marked
    // EXPERIMENTAL; this is the compiler that changes the semantics of every
    // component in the app, so it runs on the stable path until the Rust one
    // is not labelled experimental.
    babel({ presets: [reactCompilerPreset()] }),
    tailwindcss(),
  ],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "src") },
  },
  build: {
    // ../dist, not ./dist: the built bundle belongs to the APP, not to this
    // source tree. `resources/app.yml` gives Databricks Apps `app/` as its
    // source folder, so `server/`, `shared/`, `requirements.txt` and this
    // output all sit at the app root — the layout the Databricks app template
    // uses (server/ + client/), with FastAPI in place of their Node server.
    // `server/spa.py` resolves a relative dist against that app root, so
    // `DBX_FRONTEND_DIST=dist` means `app/dist` both locally and deployed.
    //
    // Keeping it out of this directory is also what lets the bundle exclude
    // `app/client/**` wholesale instead of naming build-time config files one
    // at a time to avoid swallowing the one directory that must travel.
    //
    // emptyOutDir must be explicit: Vite refuses to clear an outDir outside
    // its root without being told to, since that is how a config typo
    // deletes a source tree.
    outDir: "../dist",
    emptyOutDir: true,
    assetsDir: "assets",
    // "hidden", not true: emit the .map files but NOT the
    // `//# sourceMappingURL=` comment that points at them.
    //
    // The maps are gitignored — 5.1 MB against 1.2 MB for the bundle — so
    // they do not deploy, but with `true` the comment shipped anyway and
    // every page load asked for four maps that are not there:
    //
    //   GET /assets/index-DALehavr.js.map HTTP/1.1  404 Not Found
    //   GET /assets/three-DwIcnsOs.js.map  HTTP/1.1  404 Not Found
    //
    // "hidden" keeps them on disk for anyone debugging a local build and
    // leaves nothing to 404 in production.
    sourcemap: "hidden",
    rolldownOptions: {
      output: {
        // `three` gets its own chunk, and it must stay that way.
        //
        // It is ~600 kB before gzip and is needed by exactly one route. Left
        // to the default chunking it can be pulled into the entry as soon as
        // anything in the main graph reaches it, at which point every user of
        // every page downloads a decorative hero. `SceneBoundary` lazy-loads
        // it and this keeps the split honest;
        // `src/components/landing/chunk.test.ts` runs a real build and fails
        // if three ends up in the entry chunk.
        manualChunks: (id: string) =>
          id.includes("node_modules/three") ? "three" : undefined,
      },
    },
  },
  server: {
    // Dev runs against the real FastAPI app so the SSE path, the named
    // events and Last-Event-ID resume are exercised for real rather than
    // against a mock that cannot reproduce an ingress cut.
    proxy: apiProxy,
  },
  preview: { proxy: apiProxy },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // Vitest defaults to 5s. The dev fixture scripts generate a run's whole
    // message stream and then validate every message against the envelope
    // schema; `panel_fit / dense` is the biggest of them and takes ~2.5s on
    // its own, which is comfortably inside the default until the suite runs
    // files in parallel and it is competing for the machine. It then fails
    // intermittently, on a timeout, with nothing wrong with it — the worst
    // kind of red, because it trains people to re-run rather than look.
    testTimeout: 20_000,
    coverage: { provider: "v8", reporter: ["text", "html"] },
  },
});
