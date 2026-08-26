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
    sourcemap: true,
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
    coverage: { provider: "v8", reporter: ["text", "html"] },
  },
});
