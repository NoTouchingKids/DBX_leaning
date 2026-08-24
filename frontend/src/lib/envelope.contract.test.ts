/**
 * Drift test: `envelope.ts` against the server's own generated JSON Schema.
 *
 * `envelope.ts` is hand-written on purpose — the generated TypeScript was
 * unreadable (`RunId1`, `Seq1`, `Type1` per property occurrence) and carried
 * none of the reasoning that makes the contract usable. The cost of writing
 * it by hand is that it can silently fall behind `shared/envelope.py`. This
 * test is the thing that stops it, and it checks BOTH directions:
 *
 *   schema -> TS  every property and enum member the server can emit is
 *                 declared here (the `Object.keys` comparisons below).
 *   TS -> schema  nothing declared here is absent from the server, and the
 *                 declared types actually validate (ajv, with the schema's
 *                 own `additionalProperties: false`).
 *
 * The samples are typed as `Required<…>`, so TypeScript itself forces every
 * field to be present and rejects any field the interface does not declare.
 * That is what makes `Object.keys(sample)` a faithful stand-in for the
 * interface at runtime — TS types are erased, key sets are not.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import Ajv2020 from "ajv/dist/2020";
import { describe, expect, it } from "vitest";

import {
  LOG_LEVELS,
  MESSAGE_TYPES,
  RUN_STATUSES,
  TERMINAL_STATUSES,
  isTerminal,
  type LogMessage,
  type Message,
  type ProgressMessage,
  type ResultMessage,
  type StatusMessage,
} from "./envelope";

/**
 * The schema lives at the REPO root, outside Vite's root (`frontend/`), so it
 * cannot be imported as a module. Under jsdom `import.meta.url` is an http
 * URL, so walk up from the cwd instead — that works whether vitest is run
 * from `frontend/` or from the repo root.
 */
function findSchema(): string {
  let dir = resolve(process.cwd());
  for (;;) {
    const candidate = join(dir, "schema", "envelope.schema.json");
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) {
      throw new Error("schema/envelope.schema.json not found above " + process.cwd());
    }
    dir = parent;
  }
}

const SCHEMA_PATH = findSchema();

type JsonSchema = {
  $defs: Record<
    string,
    { enum?: string[]; properties?: Record<string, unknown> }
  >;
  discriminator: { propertyName: string; mapping: Record<string, string> };
  "x-schema-version": string;
  "x-terminal-statuses": string[];
};

const schema = JSON.parse(readFileSync(SCHEMA_PATH, "utf8")) as JsonSchema;

/** A missing `$def` is itself drift — fail loudly rather than on `undefined`. */
function def(name: string): { enum?: string[]; properties?: Record<string, unknown> } {
  const found = schema.$defs[name];
  if (!found) throw new Error(`schema has no $defs.${name}`);
  return found;
}

// `discriminator` is an OpenAPI keyword, not a JSON Schema one; ajv rejects
// unknown keywords in strict mode. The `oneOf` alongside it does the real
// validation work, so the annotation is safely ignorable.
const ajv = new Ajv2020({ strict: false, allErrors: true });
const validate = ajv.compile(schema);

/** Every field of every message type, so the key sets are comparable. */
const samples = {
  log: {
    type: "log",
    run_id: "r-1",
    seq: 0,
    ts: 1_700_000_000_000,
    message: "solving",
    level: "INFO",
    source: "model",
    phase: "solve",
    client_visible: true,
  } satisfies Required<LogMessage>,

  progress: {
    type: "progress",
    run_id: "r-1",
    seq: 1,
    ts: 1_700_000_000_001,
    elapsed_seconds: 1.5,
    percent_complete: null,
    primary_metric: 42.5,
    primary_metric_label: "incumbent",
    payload: { gap: 0.01 },
  } satisfies Required<ProgressMessage>,

  status: {
    type: "status",
    run_id: "r-1",
    seq: 2,
    ts: 1_700_000_000_002,
    status: "RUNNING",
    detail: null,
  } satisfies Required<StatusMessage>,

  result: {
    type: "result",
    run_id: "r-1",
    seq: 3,
    ts: 1_700_000_000_003,
    preview: [{ hour: 1, demand: 12 }],
    row_count: 8760,
    fetch_hint: { table: "main.dbx_leaning.results" },
    chunk_index: 0,
    final: true,
  } satisfies Required<ResultMessage>,
} satisfies Record<string, Message>;

const DEFS: Record<keyof typeof samples, string> = {
  log: "LogMessage",
  progress: "ProgressMessage",
  status: "StatusMessage",
  result: "ResultMessage",
};

describe("envelope.ts matches schema/envelope.schema.json", () => {
  it("declares every message type the server can discriminate on", () => {
    expect(Object.keys(schema.discriminator.mapping).sort()).toEqual(
      [...MESSAGE_TYPES].sort(),
    );
    expect(schema.discriminator.propertyName).toBe("type");
  });

  it("declares every log level", () => {
    expect(def("LogLevel").enum).toEqual([...LOG_LEVELS]);
  });

  it("declares every run status", () => {
    expect(def("RunStatus").enum).toEqual([...RUN_STATUSES]);
  });

  it("agrees with the server on which statuses are terminal", () => {
    expect([...schema["x-terminal-statuses"]].sort()).toEqual(
      [...TERMINAL_STATUSES].sort(),
    );
    for (const status of RUN_STATUSES) {
      expect(isTerminal(status)).toBe(
        schema["x-terminal-statuses"].includes(status),
      );
    }
  });

  it.each(Object.entries(DEFS))(
    "%s declares exactly the fields in %s",
    (kind, defName) => {
      const sample = samples[kind as keyof typeof samples];
      const schemaKeys = Object.keys(def(defName).properties ?? {}).sort();
      // Fails in one direction if the server gains a field (the interface is
      // missing it); in the other if this file invents one that is not real.
      expect(Object.keys(sample).sort()).toEqual(schemaKeys);
    },
  );

  it.each(Object.keys(samples))("validates a full %s message", (kind) => {
    const ok = validate(samples[kind as keyof typeof samples]);
    expect(validate.errors ?? []).toEqual([]);
    expect(ok).toBe(true);
  });

  it("rejects a message carrying a field the envelope does not declare", () => {
    expect(validate({ ...samples.progress, invented_field: 1 })).toBe(false);
  });

  it("accepts null percent_complete and primary_metric as real values", () => {
    // Sanitised server-side: NaN/±Infinity arrive as null. A UI that treats
    // null as "not loaded yet" renders gurobi_scheduling wrong for its whole
    // run, so this is pinned rather than assumed.
    expect(
      validate({
        ...samples.progress,
        percent_complete: null,
        primary_metric: null,
        primary_metric_label: null,
      }),
    ).toBe(true);
  });

  it("pins the schema version this client was written against", () => {
    // A bump is not a failure — it is a prompt to re-read the diff and
    // update this line deliberately.
    expect(schema["x-schema-version"]).toBe("1.0.0");
  });
});
