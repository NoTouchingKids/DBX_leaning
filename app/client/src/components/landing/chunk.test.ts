/**
 * `three` must stay out of the entry chunk.
 *
 * It is ~600 kB before gzip and is reachable from exactly one route. The
 * split is arranged in two places that cannot see each other —
 * `SceneBoundary` lazy-loads the scene, and `vite.config.ts` names a
 * `manualChunks` group — and either one can be undone without the other
 * complaining. A static `import ... from "three"` added anywhere in the main
 * graph would quietly pull it into the entry and every user of every page
 * would start paying for a decorative hero.
 *
 * Nothing short of a real build can catch that, so this runs one. It is slow
 * and it earns the time: the failure it prevents is invisible in review, in
 * the type checker and in every other test.
 */

import { readFileSync } from "node:fs";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, expect, it } from "vitest";
import { build } from "vite";

const outDir = mkdtempSync(join(tmpdir(), "dbx-chunk-"));

afterAll(() => rmSync(outDir, { recursive: true, force: true }));

interface Chunk {
  type: string;
  isEntry?: boolean;
  fileName: string;
  code?: string;
  moduleIds?: string[];
}

it(
  "does not put three in the entry chunk",
  // Third positional arg, not an options object: this vitest version types
  // the options overload as mutually exclusive with the function overload.
  async () => {
    const result = (await build({
      logLevel: "silent",
      build: { outDir, emptyOutDir: true, sourcemap: false, write: true },
    })) as unknown as Array<{ output: Chunk[] }>;

    const output = Array.isArray(result)
      ? (result[0]?.output ?? [])
      : ((result as { output: Chunk[] }).output ?? []);
    expect(output.length, "the build produced no output at all").toBeGreaterThan(0);
    const entry = output.find((c) => c.type === "chunk" && c.isEntry);
    expect(entry, "no entry chunk in the build output").toBeDefined();

    const modulesInEntry = entry?.moduleIds ?? [];
    const threeInEntry = modulesInEntry.filter((id) => id.includes("node_modules/three"));

    expect(
      threeInEntry,
      "three reached the entry chunk. Either a static `import ... from \"three\"` was " +
        "added somewhere in the main graph, or the manualChunks group in vite.config.ts " +
        "was removed. Every route now downloads ~600 kB for a decorative hero.",
    ).toEqual([]);
  },
  180_000,
);

it("emits the built SPA's entry html", () => {
  // A guard on the test above rather than a test of its own: if the build
  // silently produced nothing, the emptiness of `threeInEntry` would be
  // meaningless and the suite would pass having verified nothing.
  const html = readFileSync(join(outDir, "index.html"), "utf8");
  expect(html).toContain("<script");
});
