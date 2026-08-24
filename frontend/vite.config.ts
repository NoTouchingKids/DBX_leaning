import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
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
    react({
      // Pinned exactly, per ADR-001. The compiler is the one dependency here
      // whose output changes semantics rather than just bytes.
      babel: { plugins: [["babel-plugin-react-compiler", { target: "19" }]] },
    }),
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
