import { describe, expect, it } from "vitest";

import { MODEL_SPECS } from "@/lib/models";
import { MODEL_VIEWS, viewFor } from "./registry";

describe("model view registry", () => {
  it("binds every view to a model that exists", () => {
    // A view whose `model` does not match a spec is dead code that nothing
    // will ever render, and nothing else would report it.
    const names = new Set(MODEL_SPECS.map((spec) => spec.name));
    for (const view of MODEL_VIEWS) {
      expect(names, `${view.model} has no entry in MODEL_SPECS`).toContain(view.model);
    }
  });

  it("registers each model at most once", () => {
    const seen = MODEL_VIEWS.map((view) => view.model);
    expect(new Set(seen).size).toBe(seen.length);
  });

  it("covers every model in MODEL_SPECS", () => {
    // Not a structural requirement — the generic run page is correct for a
    // model with no view, which is why it was built first. It is a
    // completeness check: this is the assertion that fails when a tenth model
    // is added, and failing here is much cheaper than noticing on a
    // deployed page that one model looks unlike the other nine.
    const covered = new Set(MODEL_VIEWS.map((view) => view.model));
    const missing = MODEL_SPECS.filter((spec) => !covered.has(spec.name)).map((s) => s.name);
    expect(missing).toEqual([]);
  });

  it.each(MODEL_VIEWS.map((view) => [view.model, view] as const))(
    "%s carries an honesty note and at most two charts",
    (_name, view) => {
      // The honesty note is what stops a decorative animation being read as
      // data, so an empty or placeholder one makes the view incomplete.
      expect(view.honesty.trim().length).toBeGreaterThan(80);
      // Two is the cap in the contract: a third chart is a dashboard, and the
      // durable results table is the better surface for that.
      expect(view.charts.length).toBeLessThanOrEqual(2);
      expect(new Set(view.charts.map((c) => c.id)).size).toBe(view.charts.length);
    },
  );

  it("resolves by name and returns undefined for anything else", () => {
    expect(viewFor("mcmc")?.model).toBe("mcmc");
    expect(viewFor("nope")).toBeUndefined();
    expect(viewFor(undefined)).toBeUndefined();
  });
});
