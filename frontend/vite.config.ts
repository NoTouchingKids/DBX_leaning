/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import babel from "@rolldown/plugin-babel";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// The app is served by FastAPI, not a Node server — Databricks Apps has no
// Node runtime at deploy time, which is why this is a client-rendered SPA
// (ADR-001). `app/spa.py` mounts `dist/assets` at `/assets` and returns
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
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: true,
  },
  server: {
    // Dev runs against the real FastAPI app so the SSE path, the named
    // events and Last-Event-ID resume are exercised for real rather than
    // against a mock that cannot reproduce an ingress cut.
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      "/healthz": "http://127.0.0.1:8000",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    coverage: { provider: "v8", reporter: ["text", "html"] },
  },
});
