import { describe, expect, it } from "vitest";

import type { Run } from "@/lib/apiClient";

import {
  DEFAULT_FILTERS,
  DEFAULT_LIMIT,
  MAX_LIMIT,
  applyClientFilters,
  canLoadMore,
  clampLimit,
  hasServerFilter,
  historyFiltersToParams,
  matchesQuery,
  modelOptions,
  nextLimit,
  parseHistoryFilters,
  serverFilterMismatches,
  serverQuery,
  type HistoryFilters,
} from "./historyFilters";

function run(over: Partial<Run> = {}): Run {
  return {
    run_id: "run-000000000001",
    job_run_id: "9001",
    model: "mcmc",
    status: "SUCCEEDED",
    detail: null,
    started_ts: 1_700_000_000_000,
    updated_ts: 1_700_000_060_000,
    requested_by: null,
    live: false,
    ...over,
  };
}

describe("URL round-tripping", () => {
  const cases: HistoryFilters[] = [
    DEFAULT_FILTERS,
    { model: "mcmc", status: null, query: "", limit: DEFAULT_LIMIT },
    { model: null, status: "RUNNING", query: "", limit: 250 },
    { model: "gurobi_scheduling", status: "FAILED", query: "run-9c4e", limit: MAX_LIMIT },
  ];

  it.each(cases)("survives a serialise/parse cycle: %o", (filters) => {
    expect(parseHistoryFilters(historyFiltersToParams(filters))).toEqual(filters);
  });

  it("omits defaults so a model page's one-parameter link stays one parameter", () => {
    const params = historyFiltersToParams({ ...DEFAULT_FILTERS, model: "mcmc" });
    expect(params.toString()).toBe("model=mcmc");
  });

  // This is the link five model pages build by hand. If it stops working the
  // filter is applied to nothing and the page silently shows all models.
  it("reads the model-page link", () => {
    const filters = parseHistoryFilters(new URLSearchParams("model=streaming_results"));
    expect(filters.model).toBe("streaming_results");
    expect(serverQuery(filters)).toEqual({ limit: DEFAULT_LIMIT, model: "streaming_results" });
  });

  it("drops a status the server has never heard of rather than passing it on", () => {
    // The query param is an unvalidated string server-side: `status=RUNING`
    // would return zero rows, which reads as "no runs" instead of "typo".
    expect(parseHistoryFilters(new URLSearchParams("status=RUNING")).status).toBeNull();
    expect(parseHistoryFilters(new URLSearchParams("status=running")).status).toBe("RUNNING");
  });

  it("clamps a hand-edited limit instead of letting FastAPI answer 422", () => {
    expect(clampLimit(0)).toBe(1);
    expect(clampLimit(9000)).toBe(MAX_LIMIT);
    expect(clampLimit(Number.NaN)).toBe(DEFAULT_LIMIT);
    expect(parseHistoryFilters(new URLSearchParams("limit=abc")).limit).toBe(DEFAULT_LIMIT);
    expect(parseHistoryFilters(new URLSearchParams("limit=99999")).limit).toBe(MAX_LIMIT);
  });
});

describe("serverQuery", () => {
  it("sends only what the endpoint accepts — the id search has no wire form", () => {
    const query = serverQuery({ model: "mcmc", status: "RUNNING", query: "run-abc", limit: 100 });
    expect(query).toEqual({ limit: 100, status: "RUNNING", model: "mcmc" });
    expect("query" in query).toBe(false);
  });

  it("emits exactly `{limit}` when unfiltered, so it shares a React Query key with the capacity read", () => {
    expect(Object.keys(serverQuery(DEFAULT_FILTERS))).toEqual(["limit"]);
    expect(hasServerFilter(DEFAULT_FILTERS)).toBe(false);
  });
});

describe("client-side id search", () => {
  it("matches run_id and job_run_id, case-insensitively", () => {
    const row = run({ run_id: "run-9C4E21AB77D3", job_run_id: "884412" });
    expect(matchesQuery(row, "9c4e")).toBe(true);
    expect(matchesQuery(row, "884412")).toBe(true);
    expect(matchesQuery(row, "  9C4E  ")).toBe(true);
    expect(matchesQuery(row, "nope")).toBe(false);
  });

  it("an empty or whitespace query hides nothing", () => {
    expect(applyClientFilters([run(), run({ run_id: "run-b" })], { ...DEFAULT_FILTERS, query: "   " })).toHaveLength(2);
  });

  it("tolerates a null job_run_id", () => {
    expect(matchesQuery(run({ job_run_id: null }), "884412")).toBe(false);
  });
});

describe("nextLimit — the only shape 'load more' can take here", () => {
  // No offset, no cursor: a wider window is the whole mechanism. Fixed rungs
  // keep the query key stable enough for React Query to reuse a window it
  // already fetched, which `limit + 25` would not.
  it("climbs the rungs and stops at the ceiling", () => {
    expect(nextLimit(50)).toBe(100);
    expect(nextLimit(100)).toBe(250);
    expect(nextLimit(250)).toBe(500);
    expect(nextLimit(500)).toBe(500);
  });

  it("snaps an off-rung limit up to the next rung", () => {
    expect(nextLimit(60)).toBe(100);
    expect(nextLimit(1)).toBe(50);
    expect(nextLimit(9000)).toBe(500);
  });

  it("stops offering to widen at 500", () => {
    expect(canLoadMore(499)).toBe(true);
    expect(canLoadMore(500)).toBe(false);
  });
});

describe("serverFilterMismatches", () => {
  const filters: HistoryFilters = { model: "mcmc", status: "RUNNING", query: "", limit: 50 };

  it("is quiet when the echo agrees", () => {
    expect(serverFilterMismatches(filters, { model: "mcmc", status: "RUNNING" })).toEqual([]);
  });

  it("is quiet before the first response", () => {
    expect(serverFilterMismatches(filters, undefined)).toEqual([]);
  });

  // A dropped filter looks exactly like "there are no such runs", which is
  // why the echo is compared rather than assumed.
  it("names a filter the server did not apply", () => {
    expect(serverFilterMismatches(filters, { model: null, status: "RUNNING" })).toEqual(["model"]);
  });
});

describe("modelOptions", () => {
  it("keeps a selected model that this build does not know about", () => {
    // The filter is server-side, so dropping the option would reset a filter
    // that is genuinely applied.
    expect(modelOptions(["mcmc"], [], "retired_model")).toEqual(["mcmc", "retired_model"]);
  });

  it("adds models seen in the window and de-duplicates", () => {
    expect(modelOptions(["mcmc"], [run({ model: "scenario" }), run({ model: "mcmc" })], null)).toEqual([
      "mcmc",
      "scenario",
    ]);
  });
});
