/**
 * Tests for the fixtures themselves.
 *
 * Nine per-model view suites are about to be written on top of this file, so
 * a wrong fixture does not fail here — it silently teaches nine views the
 * wrong shape. The three things asserted are the three that would do that:
 * the seq contract, determinism, and each payload actually matching the
 * interface declared for it in `@/lib/models`.
 *
 * The payload validators below are written OUT LONGHAND rather than derived
 * from anything in `fixtures.ts`. A validator sharing code with the generator
 * agrees with the generator by construction and checks nothing.
 */

import { describe, expect, it } from "vitest";

import { RUN_STATUSES, type Message, type UiRunState } from "@/lib/envelope";
import { BAYESIAN_AB_STAGES, MODEL_SPECS } from "@/lib/models";
import {
  FIXTURE_NAMES,
  fixtureRunId,
  makeMessages,
  makeSnapshot,
  resetFixtureCache,
  type FixtureName,
} from "./fixtures";

const STATES: readonly UiRunState[] = ["STARTING", ...RUN_STATUSES];
const MODELS: readonly string[] = MODEL_SPECS.map((s) => s.name);

/** Every message in a snapshot, back in seq order. The snapshot is split by
 *  type; the seq contract is across the split, which is exactly why it needs
 *  reassembling to check. */
function allMessages(model: string, fixture: FixtureName, state: UiRunState | null): Message[] {
  const snap = makeSnapshot(model, fixture, state);
  return [...snap.logs, ...snap.progress, ...snap.statuses, ...snap.results].sort(
    (a, b) => a.seq - b.seq,
  );
}

/* ================================================================== *
 * 1. seq
 * ================================================================== */

describe("seq is one monotonic counter per run, shared by all message types", () => {
  for (const model of MODELS) {
    for (const fixture of FIXTURE_NAMES) {
      it(`${model} / ${fixture}`, () => {
        for (const state of STATES) {
          const messages = makeMessages(model, fixture, state);
          const seqs = messages.map((m) => m.seq);
          // Emission order IS seq order — a consumer that renders in arrival
          // order and one that sorts by seq must see the same thing.
          expect(seqs, `${state}: emission order`).toEqual([...seqs].sort((a, b) => a - b));
          expect(new Set(seqs).size, `${state}: duplicate seq`).toBe(seqs.length);
          for (let i = 1; i < seqs.length; i += 1) {
            expect(seqs[i]!, `${state}: seq must strictly increase`).toBeGreaterThan(seqs[i - 1]!);
          }
          // And it is one counter, not four: sorting the four typed arrays
          // back together must reproduce the same sequence.
          expect(allMessages(model, fixture, state).map((m) => m.seq)).toEqual(seqs);
        }
      });
    }
  }
});

it("lastSeq is the highest seq the store was given", () => {
  for (const model of MODELS) {
    for (const fixture of FIXTURE_NAMES) {
      for (const state of STATES) {
        const snap = makeSnapshot(model, fixture, state);
        const messages = makeMessages(model, fixture, state);
        const max = messages.reduce<number | null>((m, x) => (m === null || x.seq > m ? x.seq : m), null);
        expect(snap.lastSeq, `${model}/${fixture}/${state}`).toBe(max);
      }
    }
  }
});

/* ================================================================== *
 * 2. Determinism
 * ================================================================== */

describe("determinism", () => {
  it("two cold builds of the same fixture are deep-equal", () => {
    const first = new Map<string, string>();
    for (const model of MODELS) {
      for (const fixture of FIXTURE_NAMES) {
        for (const state of STATES) {
          first.set(`${model}|${fixture}|${state}`, JSON.stringify(makeMessages(model, fixture, state)));
        }
      }
    }
    // Cold, not memoised: reading the same object back would prove nothing.
    resetFixtureCache();
    for (const [key, json] of first) {
      const [model, fixture, state] = key.split("|") as [string, FixtureName, UiRunState];
      expect(JSON.stringify(makeMessages(model, fixture, state)), key).toBe(json);
    }
  });

  it("uses no wall clock — ts is fixed across builds", () => {
    const a = makeMessages("mcmc", "typical", "SUCCEEDED").map((m) => m.ts);
    resetFixtureCache();
    const b = makeMessages("mcmc", "typical", "SUCCEEDED").map((m) => m.ts);
    expect(a).toEqual(b);
    expect(a.length).toBeGreaterThan(0);
  });

  it("run ids are stable and shaped like real ones", () => {
    expect(fixtureRunId("mcmc", "typical")).toBe(fixtureRunId("mcmc", "typical"));
    expect(fixtureRunId("mcmc", "typical")).not.toBe(fixtureRunId("mcmc", "dense"));
    expect(fixtureRunId("mcmc", "typical")).toMatch(/^run-[0-9a-f]{12}$/);
  });

  it("RUNNING is a literal prefix of SUCCEEDED", () => {
    // The harness shows eight states side by side; if they were eight
    // unrelated random draws, comparing them would teach nothing.
    const running = makeMessages("annealing", "typical", "RUNNING");
    const succeeded = makeMessages("annealing", "typical", "SUCCEEDED");
    expect(running.length).toBeGreaterThan(2);
    expect(JSON.stringify(succeeded.slice(0, running.length))).toBe(JSON.stringify(running));
  });
});

/* ================================================================== *
 * 3. Payloads match the interfaces in models.ts
 * ================================================================== */

type Payload = Record<string, unknown>;

function num(p: Payload, key: string): number {
  expect(typeof p[key], `${key} should be a number, got ${JSON.stringify(p[key])}`).toBe("number");
  return p[key] as number;
}
function numOrNull(p: Payload, key: string): void {
  const v = p[key];
  expect(v === null || typeof v === "number", `${key} should be number|null, got ${JSON.stringify(v)}`).toBe(true);
}
function str(p: Payload, key: string): string {
  expect(typeof p[key], `${key} should be a string`).toBe("string");
  return p[key] as string;
}
function bool(p: Payload, key: string): void {
  expect(typeof p[key], `${key} should be a boolean`).toBe("boolean");
}
function boolOrNull(p: Payload, key: string): void {
  const v = p[key];
  expect(v === null || typeof v === "boolean", `${key} should be boolean|null`).toBe(true);
}
function numArray(p: Payload, key: string): number[] {
  const v = p[key];
  expect(Array.isArray(v), `${key} should be an array`).toBe(true);
  const arr = v as unknown[];
  for (const x of arr) expect(typeof x, `${key}[] entries`).toBe("number");
  return arr as number[];
}

/** One validator per model, transcribed by hand from the interfaces in
 *  `models.ts`. `i` is the index of this progress message within the run. */
const VALIDATORS: Record<string, (p: Payload, i: number) => void> = {
  // GurobiProgressPayload
  gurobi_scheduling: (p) => {
    num(p, "best_bound");
    num(p, "incumbent");
    num(p, "nodes_explored");
    num(p, "nodes_remaining");
    num(p, "solution_count");
  },
  // GurobiRoutingProgressPayload = GurobiProgressPayload
  gurobi_routing: (p) => {
    num(p, "best_bound");
    num(p, "incumbent");
    num(p, "nodes_explored");
    num(p, "nodes_remaining");
    num(p, "solution_count");
  },
  // ForecastingProgressPayload. val_loss is deliberately NOT here — it is
  // primary_metric.
  forecasting: (p) => {
    num(p, "epoch");
    num(p, "epochs_total");
    num(p, "train_loss");
    num(p, "best_val_loss");
    num(p, "learning_rate");
    boolOrNull(p, "data_synthetic");
    expect(p).not.toHaveProperty("val_loss");
  },
  // McmcProgressPayload
  mcmc: (p) => {
    num(p, "draws_done");
    num(p, "draws_total");
    const chains = num(p, "chains");
    const params = p.parameters;
    expect(Array.isArray(params)).toBe(true);
    for (const x of params as unknown[]) expect(typeof x).toBe("string");
    num(p, "post_burn_in_draws");
    num(p, "mean_acceptance");
    num(p, "min_acceptance");
    num(p, "stuck_chains");
    expect(numArray(p, "per_chain_acceptance")).toHaveLength(chains);
  },
  // ScenarioProgressPayload — last_scenario / last_outcome are `unknown` in
  // the interface, so only presence is contract.
  scenario: (p) => {
    num(p, "scenarios_done");
    num(p, "scenarios_total");
    expect(p).toHaveProperty("last_scenario");
    expect(p).toHaveProperty("last_outcome");
  },
  // StreamingProgressPayload — three named keys plus an index signature.
  streaming_results: (p) => {
    num(p, "windows_done");
    num(p, "windows_total");
    num(p, "origin");
    // The provenance spread is what the index signature is for; assert only
    // that extras exist, never their names.
    expect(Object.keys(p).length).toBeGreaterThan(3);
  },
  // AnnealingProgressPayload
  annealing: (p) => {
    num(p, "iteration");
    num(p, "iterations_total");
    num(p, "temperature");
    num(p, "current_objective");
    num(p, "current_value");
    num(p, "current_weight");
    num(p, "capacity");
    bool(p, "feasible");
    num(p, "acceptance_rate");
    num(p, "accepted_total");
    num(p, "items_selected");
  },
  // BayesianAbProgressPayload
  bayesian_ab: (p, i) => {
    expect(BAYESIAN_AB_STAGES as readonly string[]).toContain(str(p, "stage"));
    // 1-based, and a count of completed stages — `enumerate(STAGES, start=1)`
    // in the model, emitting after each stage body runs. `i` is the array
    // index of the message, so the wire value is one higher.
    expect(num(p, "stage_index")).toBe(i + 1);
    expect(num(p, "stages_total")).toBe(BAYESIAN_AB_STAGES.length);
    expect(p.progress_shape).toBe("stages");
    str(p, "comparison");
    str(p, "outcome");
    const prior = p.prior as Payload;
    num(prior, "alpha");
    num(prior, "beta");
    num(p, "credible_mass");
    const arms = p.arms as Payload[];
    expect(arms).toHaveLength(2);
    expect(arms.map((a) => a.role)).toEqual(["A", "B"]);
    for (const arm of arms) {
      str(arm, "label");
      num(arm, "trials");
      num(arm, "successes");
      numOrNull(arm, "posterior_alpha");
      numOrNull(arm, "posterior_beta");
      numOrNull(arm, "posterior_mean");
    }
    // ABSENT, not null, until their stage has run — `in`, never `=== null`.
    expect("prob_b_beats_a" in p).toBe(i >= 1);
    expect("expected_loss" in p).toBe(i >= 2);
    expect("lift" in p).toBe(i >= 3);
    expect("decision" in p).toBe(i >= 4);
    expect("conclusive" in p).toBe(i >= 4);
    if (i >= 4) {
      // An arm LABEL or the literal "inconclusive" — never "A"/"B".
      const labels = arms.map((a) => a.label as string);
      expect([...labels, "inconclusive"]).toContain(p.decision);
      bool(p, "conclusive");
    }
  },
  // NeuralNetProgressPayload
  neural_net: (p) => {
    expect(["epoch", "batch"]).toContain(str(p, "level"));
    num(p, "epoch");
    num(p, "epochs_total");
    num(p, "batch");
    num(p, "batches_per_epoch");
    num(p, "train_loss");
    // Unlike forecasting, val_loss IS in this payload.
    num(p, "val_loss");
    num(p, "macro_f1");
    num(p, "grad_norm");
    num(p, "learning_rate");
    numOrNull(p, "best_val_accuracy");
    num(p, "baseline_accuracy");
    str(p, "device");
    boolOrNull(p, "data_synthetic");
  },
};

describe("per-model progress payloads match their declared interfaces", () => {
  for (const model of MODELS) {
    it(model, () => {
      const validate = VALIDATORS[model];
      expect(validate, `no validator written for ${model}`).toBeDefined();
      let seen = 0;
      for (const fixture of FIXTURE_NAMES) {
        for (const state of STATES) {
          const snap = makeSnapshot(model, fixture, state);
          snap.progress.forEach((msg, i) => {
            validate!(msg.payload, i);
            seen += 1;
          });
        }
      }
      expect(seen, "no progress messages were checked at all").toBeGreaterThan(0);
    });
  }

  it("every model in MODEL_SPECS has a hand-written script, not the fallback", () => {
    for (const model of MODELS) {
      const payloads = makeSnapshot(model, "typical", "SUCCEEDED").progress;
      expect(payloads.length, model).toBeGreaterThan(0);
      expect(Object.keys(payloads[0]!.payload).length, model).toBeGreaterThan(0);
    }
  });
});

describe("common envelope fields", () => {
  it("are populated on every message of every fixture", () => {
    for (const model of MODELS) {
      for (const fixture of FIXTURE_NAMES) {
        const messages = makeMessages(model, fixture, "SUCCEEDED");
        for (const m of messages) {
          expect(m.run_id).toBe(fixtureRunId(model, fixture));
          expect(Number.isFinite(m.ts)).toBe(true);
          expect(Number.isInteger(m.seq)).toBe(true);
          if (m.type === "progress") {
            // Sanitised server-side: never NaN, never Infinity.
            expect(m.primary_metric === null || Number.isFinite(m.primary_metric)).toBe(true);
            expect(m.percent_complete === null || Number.isFinite(m.percent_complete)).toBe(true);
            expect(Number.isFinite(m.elapsed_seconds)).toBe(true);
          }
        }
      }
    }
  });

  it("the generic fallback emits common fields only, which is the generic view's input", () => {
    const snap = makeSnapshot("a_model_nobody_has_written_yet", "typical", "RUNNING");
    expect(snap.progress.length).toBeGreaterThan(0);
    for (const msg of snap.progress) {
      expect(msg.payload).toEqual({});
      expect(typeof msg.percent_complete).toBe("number");
      expect(typeof msg.primary_metric).toBe("number");
    }
  });
});

/* ================================================================== *
 * 4. The awkward cases, by name
 * ================================================================== */

describe("empty — a terminal run observed with zero messages", () => {
  it("has nothing in it at all", () => {
    const snap = makeSnapshot("bayesian_ab", "empty", "SUCCEEDED");
    expect(snap.logs).toHaveLength(0);
    expect(snap.progress).toHaveLength(0);
    expect(snap.statuses).toHaveLength(0);
    expect(snap.results).toHaveLength(0);
    expect(snap.latestProgress).toBeNull();
    expect(snap.lastSeq).toBeNull();
  });

  it("still knows it is terminal — that came from GET /api/runs, not the stream", () => {
    const snap = makeSnapshot("bayesian_ab", "empty", "SUCCEEDED");
    expect(snap.terminal).toBe(true);
    expect(snap.status).toBe("SUCCEEDED");
    // Hydration HAPPENED and returned nothing, which is not the same fact as
    // "not read yet".
    expect(snap.hydrated).toBe(true);
  });

  it("is distinct from state null, where nothing has been read yet", () => {
    const none = makeSnapshot("bayesian_ab", "empty", null);
    expect(none.hydrated).toBe(false);
    expect(none.status).toBeNull();
    expect(none.terminal).toBe(false);
  });
});

describe("sparse — a whole run in a handful of messages", () => {
  it("gives every model at most eight progress points", () => {
    for (const model of MODELS) {
      const snap = makeSnapshot(model, "sparse", "SUCCEEDED");
      expect(snap.progress.length, model).toBeLessThanOrEqual(8);
      expect(snap.progress.length, model).toBeGreaterThan(0);
    }
  });
});

describe("dense — the Recharts stress case", () => {
  it("is thousands of points for models that can produce them", () => {
    expect(makeSnapshot("mcmc", "dense", "SUCCEEDED").progress.length).toBeGreaterThan(1000);
  });

  it("does not invent a sixth bayesian_ab stage to hit a number", () => {
    // Faithful over uniform: five stages exist, so five is what dense means
    // for this model.
    expect(makeSnapshot("bayesian_ab", "dense", "SUCCEEDED").progress).toHaveLength(
      BAYESIAN_AB_STAGES.length,
    );
  });
});

describe("null-heavy — null is a value, not a loading state", () => {
  it("nulls percent_complete, primary_metric and its label throughout", () => {
    for (const model of MODELS) {
      const snap = makeSnapshot(model, "null-heavy", "SUCCEEDED");
      expect(snap.progress.length, model).toBeGreaterThan(0);
      for (const msg of snap.progress) {
        expect(msg.percent_complete, model).toBeNull();
        expect(msg.primary_metric, model).toBeNull();
        expect(msg.primary_metric_label, model).toBeNull();
      }
    }
  });

  it("also nulls the nullable payload fields, which is where views forget", () => {
    for (const msg of makeSnapshot("neural_net", "null-heavy", "SUCCEEDED").progress) {
      expect(msg.payload.best_val_accuracy).toBeNull();
    }
    for (const msg of makeSnapshot("bayesian_ab", "null-heavy", "SUCCEEDED").progress) {
      for (const arm of msg.payload.arms as Payload[]) {
        expect(arm.posterior_mean).toBeNull();
      }
    }
  });

  it("leaves gurobi_scheduling null even on a normal fixture — it always is", () => {
    for (const msg of makeSnapshot("gurobi_scheduling", "typical", "SUCCEEDED").progress) {
      expect(msg.percent_complete).toBeNull();
      expect(msg.primary_metric_label).toBe("mip_gap");
    }
  });
});

describe("chunked — append, never replace", () => {
  it("emits rising chunk_index with exactly one final:true, last", () => {
    const snap = makeSnapshot("streaming_results", "chunked", "SUCCEEDED");
    expect(snap.results.length).toBeGreaterThan(1);
    expect(snap.results.map((r) => r.chunk_index)).toEqual(
      snap.results.map((_, i) => i),
    );
    expect(snap.results.filter((r) => r.final)).toHaveLength(1);
    expect(snap.results.at(-1)!.final).toBe(true);
    // chunk_index is NOT seq — the two must not be confused.
    expect(snap.results.map((r) => r.seq)).not.toEqual(snap.results.map((r) => r.chunk_index));
  });

  it("a still-running chunked run has chunks but no final one", () => {
    const snap = makeSnapshot("streaming_results", "chunked", "RUNNING");
    expect(snap.results.length).toBeGreaterThan(0);
    expect(snap.results.some((r) => r.final)).toBe(false);
  });
});

describe("gappy — a hole that never closes", () => {
  it("reports the gap and really does skip those seq numbers", () => {
    const snap = makeSnapshot("scenario", "gappy", "SUCCEEDED");
    expect(snap.gaps.length).toBeGreaterThan(0);
    const gap = snap.gaps[0]!;
    const seqs = new Set(allMessages("scenario", "gappy", "SUCCEEDED").map((m) => m.seq));
    for (let s = gap.from; s <= gap.to; s += 1) {
      expect(seqs.has(s), `seq ${s} should be missing`).toBe(false);
    }
    // Bracketed by messages that DID arrive, so it is a hole and not a
    // truncation.
    expect(seqs.has(gap.from - 1)).toBe(true);
    expect(seqs.has(gap.to + 1)).toBe(true);
  });

  it("carries client_visible:false logs, which only backfill ever returns", () => {
    const snap = makeSnapshot("scenario", "gappy", "SUCCEEDED");
    expect(snap.logs.some((l) => !l.client_visible)).toBe(true);
  });

  it("no other fixture invents a gap", () => {
    for (const fixture of FIXTURE_NAMES) {
      if (fixture === "gappy") continue;
      expect(makeSnapshot("scenario", fixture, "SUCCEEDED").gaps, fixture).toHaveLength(0);
    }
  });
});

/* ================================================================== *
 * 5. Lifecycle coverage
 * ================================================================== */

describe("lifecycle states", () => {
  it("STARTING has seen nothing — no status, no progress", () => {
    const snap = makeSnapshot("forecasting", "typical", "STARTING");
    expect(snap.statuses).toHaveLength(0);
    expect(snap.progress).toHaveLength(0);
    expect(snap.connection).toBe("connecting");
  });

  it("QUEUED has a status and nothing else", () => {
    const snap = makeSnapshot("forecasting", "typical", "QUEUED");
    expect(snap.status).toBe("QUEUED");
    expect(snap.terminal).toBe(false);
    expect(snap.progress).toHaveLength(0);
  });

  it("every terminal state reports itself terminal, on every model", () => {
    for (const model of MODELS) {
      for (const state of ["SUCCEEDED", "FAILED", "CANCELLED", "INFEASIBLE"] as const) {
        const snap = makeSnapshot(model, "typical", state);
        expect(snap.status, `${model}/${state}`).toBe(state);
        expect(snap.terminal, `${model}/${state}`).toBe(true);
        expect(snap.connection, `${model}/${state}`).toBe("idle");
      }
    }
  });

  it("FAILED never reached the result write; INFEASIBLE did and wrote nothing", () => {
    expect(makeSnapshot("gurobi_routing", "typical", "FAILED").results).toHaveLength(0);
    const infeasible = makeSnapshot("gurobi_routing", "typical", "INFEASIBLE");
    expect(infeasible.results).toHaveLength(1);
    // row_count 0 is the whole point: "got there, wrote nothing" is a
    // different fact from "never got there".
    expect(infeasible.results[0]!.row_count).toBe(0);
    expect(infeasible.results[0]!.final).toBe(true);
  });

  it("CANCELLED keeps its incumbent", () => {
    const snap = makeSnapshot("annealing", "typical", "CANCELLED");
    expect(snap.results).toHaveLength(1);
    expect(snap.results[0]!.row_count).toBeGreaterThan(0);
    expect(snap.results[0]!.final).toBe(true);
  });

  it("latestProgress is the highest-seq progress message", () => {
    const snap = makeSnapshot("mcmc", "typical", "SUCCEEDED");
    expect(snap.latestProgress).toBe(snap.progress.at(-1));
  });
});
