import { describe, expect, it } from "vitest";

import type { LogMessage } from "@/lib/envelope";
import { deriveLogFacets, filterLogs, EMPTY_FILTERS } from "./logFilters";

function log(seq: number, over: Partial<LogMessage> = {}): LogMessage {
  return {
    type: "log",
    run_id: "run-a",
    seq,
    ts: 1_700_000_000_000 + seq,
    message: `line ${seq}`,
    level: "INFO",
    source: "model",
    phase: "run",
    client_visible: true,
    ...over,
  };
}

describe("deriveLogFacets", () => {
  it("takes source and phase from what the run emitted, not a fixed list", () => {
    // Neither field is constrained by the envelope: `source` is open in
    // practice and `phase` is free text each model picks. A hardcoded list
    // would hide every line from a model that chose a new value.
    const facets = deriveLogFacets([
      log(1, { source: "job", phase: "input" }),
      log(2, { source: "gurobi", phase: "solve" }),
      log(3, { source: "model", phase: "solve" }),
      log(4, { source: "cuda-sampler", phase: "warmup" }),
    ]);

    expect(facets.sources).toEqual(["cuda-sampler", "gurobi", "job", "model"]);
    expect(facets.phases).toEqual(["input", "solve", "warmup"]);
  });

  it("counts the fixed level enum, including levels the run never emitted", () => {
    const facets = deriveLogFacets([log(1), log(2, { level: "ERROR" })]);
    expect(facets.levelCounts).toEqual({ DEBUG: 0, INFO: 1, WARNING: 0, ERROR: 1 });
  });

  it("has empty options for a run that has logged nothing", () => {
    expect(deriveLogFacets([])).toEqual({
      sources: [],
      phases: [],
      levelCounts: { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0 },
    });
  });
});

describe("filterLogs", () => {
  const logs = [
    log(1, { level: "DEBUG", source: "job", phase: "input", message: "loading demand" }),
    log(2, { level: "INFO", source: "gurobi", phase: "solve", message: "incumbent 42880" }),
    log(3, { level: "ERROR", source: "model", phase: "solve", message: "no feasible schedule" }),
  ];

  it("passes everything through with no filters set", () => {
    expect(filterLogs(logs, EMPTY_FILTERS)).toHaveLength(3);
  });

  it("treats an empty level set as no filter, not as hide-everything", () => {
    expect(filterLogs(logs, { ...EMPTY_FILTERS, levels: new Set() })).toHaveLength(3);
  });

  it("filters by level, source, phase and a case-insensitive search", () => {
    expect(filterLogs(logs, { ...EMPTY_FILTERS, levels: new Set(["ERROR"]) })).toHaveLength(1);
    expect(filterLogs(logs, { ...EMPTY_FILTERS, source: "gurobi" })).toHaveLength(1);
    expect(filterLogs(logs, { ...EMPTY_FILTERS, phase: "solve" })).toHaveLength(2);
    expect(filterLogs(logs, { ...EMPTY_FILTERS, search: "INCUMBENT" })).toHaveLength(1);
  });

  it("drops duplicate seq values", () => {
    // The transport re-sends a run's whole IndexedDB history as a `hydrate`
    // batch on every subscribe, and RunStore.ingest appends without checking,
    // so re-entering a run page appends a second copy of every line.
    expect(filterLogs([log(1), log(2), log(1), log(2)], EMPTY_FILTERS)).toHaveLength(2);
  });
});
