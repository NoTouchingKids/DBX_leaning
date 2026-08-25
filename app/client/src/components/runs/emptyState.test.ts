import { describe, expect, it } from "vitest";

import { describeEmptyState } from "./emptyState";
import { DEFAULT_FILTERS } from "./historyFilters";

describe("describeEmptyState", () => {
  it("says 'no runs yet' only when nothing was filtered", () => {
    expect(describeEmptyState(DEFAULT_FILTERS, 0).title).toBe("No runs yet");
  });

  it("blames the server-side filter, and says it narrowed the whole history", () => {
    const copy = describeEmptyState({ ...DEFAULT_FILTERS, status: "INFEASIBLE" }, 0);
    expect(copy.title).toBe("No runs match");
    expect(copy.body).toContain("status = INFEASIBLE");
    expect(copy.body).toContain("server-side");
  });

  it("names both server-side filters when both are set", () => {
    const copy = describeEmptyState({ ...DEFAULT_FILTERS, status: "FAILED", model: "mcmc" }, 0);
    expect(copy.body).toContain("status = FAILED");
    expect(copy.body).toContain("model = mcmc");
  });

  // The distinction that matters: rows came back and the browser hid them, so
  // the instruction is "widen the window", not "there are none".
  it("blames the client-side search when the server did return rows", () => {
    const copy = describeEmptyState({ ...DEFAULT_FILTERS, query: "run-zzz" }, 50);
    expect(copy.title).toBe("No match in this window");
    expect(copy.body).toContain("run-zzz");
    expect(copy.body).toContain("widen the window");
  });

  it("does not blame the search when the server returned nothing either", () => {
    expect(describeEmptyState({ ...DEFAULT_FILTERS, query: "run-zzz" }, 0).title).toBe(
      "No runs yet",
    );
  });
});
