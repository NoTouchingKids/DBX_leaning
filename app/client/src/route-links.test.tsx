/**
 * Every link in the app must land on a route.
 *
 * This exists because of a specific bug. Moving the models under `job/` in the
 * source tree rewrote `<Route path="models/:model">` to
 * `<Route path="job/models/:model">` — a filesystem edit applied to a URL. The
 * app kept building, every test kept passing, and every model link in the
 * product went to "Not found": the sidebar, the home page's cards, the runs
 * table, the history notices. Nothing typed the link against the route.
 *
 * So these tests do. They read the route table out of `App.tsx` and the link
 * targets out of the components that build them, and match one against the
 * other — the two halves that have to agree and that TypeScript cannot make
 * agree, since both are strings.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { MODEL_SPECS } from "@/lib/models";

const HERE = dirname(fileURLToPath(import.meta.url));

function source(...parts: string[]): string {
  return readFileSync(join(HERE, ...parts), "utf8");
}

/** The `path=` of every `<Route>` in App.tsx, normalised to a leading slash. */
function declaredRoutes(): string[] {
  const app = source("App.tsx");
  const paths = [...app.matchAll(/<Route\s+path="([^"]+)"/g)].flatMap((m) => m[1] ?? []);
  expect(paths.length).toBeGreaterThan(3);
  return paths.map((p) => (p.startsWith("/") ? p : `/${p}`));
}

/** Does any declared route match this concrete path? */
function resolves(path: string): boolean {
  const segments = path.split("/").filter(Boolean);
  // `<Route index>` carries no `path=`, so the root is matched here.
  if (segments.length === 0) return true;
  return declaredRoutes().some((route) => {
    if (route === "/*") return false; // the NotFound catch-all is not a match
    const pattern = route.split("/").filter(Boolean);
    if (pattern.length !== segments.length) return false;
    return pattern.every((part, i) => part.startsWith(":") || part === segments[i]);
  });
}

describe("the model page", () => {
  it("has a route", () => {
    expect(declaredRoutes()).toContain("/models/:model");
  });

  it.each(MODEL_SPECS.map((s) => s.name))("resolves for %s", (name) => {
    expect(resolves(`/models/${name}`)).toBe(true);
  });

  it("is not addressed by its place in the source tree", () => {
    // `job/models/` is where the Python lives. It is not a URL, and the two
    // being spelled alike is exactly what let one become the other.
    expect(declaredRoutes()).not.toContain("/job/models/:model");
  });
});

describe("every link target in the app", () => {
  /** Files that build a link to a model page, and what they build. */
  const LINKING = [
    "components/layout/Sidebar.tsx",
    "components/rundetail/HistoryNotices.tsx",
    "components/runs/RunsTable.tsx",
    "pages/HomePage.tsx",
  ];

  it.each(LINKING)("%s points somewhere routable", (file) => {
    const text = source(file);
    // Everything a `to=` attribute could resolve to, including the branches of
    // a ternary — RunsTable picks between two paths inline, and matching only
    // the literal right after `to=` silently skipped both.
    const links = text
      .split("to=")
      .slice(1)
      .flatMap((chunk) =>
        [...chunk.slice(0, 200).matchAll(/[`"']([^`"']*)[`"']/g)].flatMap((m) => m[1] ?? []),
      )
      .filter((href) => href.startsWith("/"));

    expect(links.length).toBeGreaterThan(0);
    for (const href of links) {
      // `${...}` stands in for one path segment.
      const concrete = href.replace(/\$\{[^}]*\}/g, "placeholder").replace(/\?.*$/, "");
      expect(resolves(concrete), `${file} links to ${href}, which no route matches`).toBe(true);
    }
  });
});
