/**
 * The trigger form, generated from a model's `ConfigField[]`.
 *
 * One form for every model, driven entirely by `MODEL_SPECS` — which is being
 * extended from five entries to nine on another track, so nothing here may
 * branch on a model name.
 *
 * Native `<details>` for the advanced disclosure and native inputs
 * throughout: this form has no combobox, no multi-select and no custom
 * keyboard behaviour, so a headless primitive library would only be adding a
 * dependency and re-implementing focus handling the platform already has.
 */

import { useMemo, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { Card } from "@/components/ui/Card";
import { isApiError } from "@/lib/apiClient";
import type { ConfigField, ModelSpec } from "@/lib/models";
import {
  buildConfig,
  initialValues,
  placeholderFor,
  type FormValues,
} from "./triggerConfig";

function Field({
  field,
  value,
  error,
  onChange,
}: {
  field: ConfigField;
  value: string | boolean;
  error?: string;
  onChange: (next: string | boolean) => void;
}) {
  const optional = field.default === undefined;
  const id = `cfg-${field.key}`;

  if (field.kind === "bool") {
    return (
      <label htmlFor={id} className="flex cursor-pointer items-center gap-2 py-1 text-[0.78rem]">
        <input
          id={id}
          type="checkbox"
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
          className="h-4 w-4 accent-[var(--c-accent)]"
        />
        <span>{field.label}</span>
        <code className="ml-auto text-[0.63rem] text-faint">{field.key}</code>
      </label>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="flex items-baseline justify-between gap-2 text-[0.74rem]">
        <span>{field.label}</span>
        <code className="text-[0.63rem] text-faint">{field.key}</code>
      </label>
      <input
        id={id}
        type={field.kind === "int" || field.kind === "float" ? "number" : "text"}
        step={field.kind === "float" ? "any" : field.kind === "int" ? "1" : undefined}
        inputMode={field.kind === "int" ? "numeric" : field.kind === "float" ? "decimal" : undefined}
        value={typeof value === "string" ? value : ""}
        placeholder={placeholderFor(field)}
        aria-describedby={field.hint ? `${id}-hint` : undefined}
        aria-invalid={error !== undefined || undefined}
        onChange={(event) => onChange(event.target.value)}
        className={
          `w-full rounded-md border bg-paper px-2 py-1.5 font-mono text-[0.78rem] ` +
          (error !== undefined
            ? "border-bad"
            : optional
              ? "border-dashed border-edge placeholder:text-faint placeholder:italic"
              : "border-edge")
        }
      />
      {error !== undefined ? (
        <span className="text-[0.68rem] text-bad">{error}</span>
      ) : (
        field.hint !== undefined && (
          <span id={`${id}-hint`} className="text-[0.66rem] text-faint">
            {field.hint}
          </span>
        )
      )}
    </div>
  );
}

export function TriggerForm({
  spec,
  triggerable,
  pending,
  error,
  onSubmit,
  cancelSlot,
}: {
  spec: ModelSpec;
  /** From `GET /api/models`. A model can exist in `job/models/` with no job
   *  behind it, in which case `POST /api/runs` answers 404. */
  triggerable: boolean | undefined;
  pending: boolean;
  error: unknown;
  onSubmit: (config: Record<string, unknown>) => void;
  cancelSlot?: React.ReactNode;
}) {
  // The caller mounts this with `key={spec.name}`, so switching models
  // remounts rather than carrying one model's numbers into another's fields.
  const [values, setValues] = useState<FormValues>(() => initialValues(spec));
  const [errors, setErrors] = useState<Record<string, string>>({});

  const [basic, advanced] = useMemo(
    () => [
      spec.fields.filter((f) => f.advanced !== true),
      spec.fields.filter((f) => f.advanced === true),
    ],
    [spec],
  );

  function set(key: string, next: string | boolean) {
    setValues((prev) => ({ ...prev, [key]: next }));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const built = buildConfig(spec.fields, values);
    setErrors(built.errors);
    if (Object.keys(built.errors).length > 0) return;
    onSubmit(built.config);
  }

  return (
    <Card title="Run configuration" hint="→ config">
      <form onSubmit={submit} className="flex flex-col gap-3">
        {basic.map((field) => (
          <Field
            key={field.key}
            field={field}
            value={values[field.key] ?? ""}
            error={errors[field.key]}
            onChange={(next) => set(field.key, next)}
          />
        ))}

        {advanced.length > 0 && (
          <details className="border-t border-dashed border-edge pt-3">
            <summary className="cursor-pointer text-[0.76rem] font-semibold text-dim marker:text-accent">
              Advanced — {advanced.length} field{advanced.length === 1 ? "" : "s"}
            </summary>
            <div className="mt-3 flex flex-col gap-3">
              {advanced.map((field) => (
                <Field
                  key={field.key}
                  field={field}
                  value={values[field.key] ?? ""}
                  error={errors[field.key]}
                  onChange={(next) => set(field.key, next)}
                />
              ))}
            </div>
            <p className="mt-2 text-[0.66rem] leading-relaxed text-faint">
              Left blank, these keys are omitted from the request entirely —
              which is what selects the model&apos;s own behaviour. Sending a
              zero would not.
            </p>
          </details>
        )}

        <div className="mt-1 flex flex-col gap-2">
          <Button type="submit" variant="primary" disabled={pending || triggerable === false}>
            {pending ? "Starting…" : "▷ Start run"}
          </Button>
          {cancelSlot}
        </div>

        {triggerable === false && (
          <Callout tone="warn" title="Not triggerable from this app">
            {`${spec.name} is defined in job/models/ but GET /api/models does not list it, so no Databricks job is configured for it. POST /api/runs would answer 404.`}
          </Callout>
        )}

        {/*
          The server's own words, verbatim. A 429 body names the current
          concurrent-task count and the account-wide ceiling of 5 — the single
          most likely error on Free Edition — and a 404 names the models that
          ARE triggerable. Both are more useful than anything written here.
        */}
        {error !== null && error !== undefined && (
          <Callout
            tone={isApiError(error) && error.status === 429 ? "warn" : "bad"}
            title={isApiError(error) ? `Refused (HTTP ${error.status})` : "Could not start the run"}
          >
            {isApiError(error) ? error.detail : String(error)}
          </Callout>
        )}

        <div className="text-[0.66rem] text-faint">
          POST <code>/api/runs</code> · model <code>{spec.name}</code>
        </div>
      </form>
    </Card>
  );
}
