/**
 * Turning a form into a `TriggerRequest.config`.
 *
 * `config` is passed verbatim into `DBX_MODEL_CONFIG` and validated by
 * nothing — not by the app, not by a schema. The model reads it with
 * `cfg.get(key, default)`. That makes ONE rule load-bearing:
 *
 *   **A blank field is an omitted key, never a zero.**
 *
 * For the fields with no model-side default (`time_limit_s`, `mip_gap`,
 * `trips_per_staff`, `n`, `series`, `data`) an absent key means "auto" / "no
 * limit" / "solver default", and a `0` means something entirely different and
 * usually catastrophic: `time_limit_s: 0` is a solve that gives up
 * immediately, `mip_gap: 0` is one that never stops. There is no server-side
 * check that would catch either.
 *
 * The dotted keys in `MODEL_SPECS` are form labels, not wire keys: scenario's
 * model does a single `cfg.get("grid")` and reads three lists out of it, so
 * `grid.demand` has to be nested back into `{ grid: { demand: [...] } }`
 * rather than sent as a flat `"grid.demand"` key the model never looks for.
 */

import type { ConfigField, ModelSpec } from "@/lib/models";

/** Booleans are checkboxes; everything else stays a raw string so that ""
 *  (blank, meaning "omit") stays distinguishable from "0" (a real value). */
export type FieldValue = string | boolean;
export type FormValues = Record<string, FieldValue>;

export function initialValues(spec: ModelSpec): FormValues {
  const values: FormValues = {};
  for (const field of spec.fields) {
    if (field.kind === "bool") {
      values[field.key] = field.default === true;
    } else if (Array.isArray(field.default)) {
      values[field.key] = field.default.join(", ");
    } else {
      values[field.key] = field.default === undefined ? "" : String(field.default);
    }
  }
  return values;
}

/** What a blank optional field means, for the placeholder text. */
export function placeholderFor(field: ConfigField): string {
  if (field.default !== undefined) return String(field.default);
  return field.hint?.match(/omit for (.+?)\.?$/i)?.[1] ?? "omitted";
}

export interface BuiltConfig {
  config: Record<string, unknown>;
  /** Keyed by field key. Non-empty means do not submit. */
  errors: Record<string, string>;
}

function assign(target: Record<string, unknown>, dottedKey: string, value: unknown): void {
  const parts = dottedKey.split(".");
  let cursor = target;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const part = parts[i] as string;
    const existing = cursor[part];
    if (existing === undefined || typeof existing !== "object" || existing === null) {
      cursor[part] = {};
    }
    cursor = cursor[part] as Record<string, unknown>;
  }
  cursor[parts[parts.length - 1] as string] = value;
}

function parseNumberList(raw: string): number[] | null {
  const parts = raw.split(/[\s,]+/u).filter((p) => p.length > 0);
  const numbers = parts.map(Number);
  return numbers.some((n) => !Number.isFinite(n)) ? null : numbers;
}

export function buildConfig(fields: readonly ConfigField[], values: FormValues): BuiltConfig {
  const config: Record<string, unknown> = {};
  const errors: Record<string, string> = {};

  for (const field of fields) {
    const raw = values[field.key];

    if (field.kind === "bool") {
      // A checkbox has no blank state, so there is nothing to omit.
      assign(config, field.key, raw === true);
      continue;
    }

    const text = typeof raw === "string" ? raw.trim() : "";
    if (text === "") continue; // omitted, deliberately — see the module note

    if (field.kind === "string") {
      assign(config, field.key, text);
      continue;
    }

    if (field.kind === "number-list") {
      const list = parseNumberList(text);
      if (list === null) errors[field.key] = "expected numbers separated by commas or spaces";
      else if (list.length > 0) assign(config, field.key, list);
      continue;
    }

    const value = Number(text);
    if (!Number.isFinite(value)) {
      errors[field.key] = "expected a number";
      continue;
    }
    if (field.kind === "int" && !Number.isInteger(value)) {
      errors[field.key] = "expected a whole number";
      continue;
    }
    assign(config, field.key, value);
  }

  return { config, errors };
}
