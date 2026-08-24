import { describe, expect, it } from "vitest";

import { GUROBI_SCHEDULING, SCENARIO, type ConfigField } from "@/lib/models";
import { buildConfig, initialValues } from "./triggerConfig";

describe("buildConfig", () => {
  it("omits a blank optional field instead of sending zero", () => {
    // `time_limit_s` and `mip_gap` have no model-side default: omitting them
    // means "no limit" / "solver default". Sending 0 means "give up
    // immediately" / "never stop" — the exact opposite, with nothing
    // server-side to catch it.
    const values = initialValues(GUROBI_SCHEDULING);
    const { config, errors } = buildConfig(GUROBI_SCHEDULING.fields, values);

    expect(errors).toEqual({});
    expect(config).not.toHaveProperty("time_limit_s");
    expect(config).not.toHaveProperty("mip_gap");
    expect(config).not.toHaveProperty("trips_per_staff");
    expect(config.staff_count).toBe(20);
  });

  it("sends a zero the user actually typed", () => {
    const fields: ConfigField[] = [{ key: "mip_gap", label: "gap", kind: "float" }];
    expect(buildConfig(fields, { mip_gap: "0" }).config).toEqual({ mip_gap: 0 });
    expect(buildConfig(fields, { mip_gap: "" }).config).toEqual({});
    expect(buildConfig(fields, { mip_gap: "   " }).config).toEqual({});
  });

  it("clearing a field that HAS a default omits it too, deferring to the model", () => {
    const fields: ConfigField[] = [{ key: "days", label: "Days", kind: "int", default: 14 }];
    expect(buildConfig(fields, { days: "" }).config).toEqual({});
  });

  it("always sends booleans — a checkbox has no blank state", () => {
    const fields: ConfigField[] = [
      { key: "use_sample_data", label: "x", kind: "bool", default: true },
    ];
    expect(buildConfig(fields, { use_sample_data: false }).config).toEqual({
      use_sample_data: false,
    });
  });

  it("nests dotted keys back into the single key the model reads", () => {
    // scenario's model does one `cfg.get("grid")` and reads three lists out of
    // it. A flat "grid.demand" key would be silently ignored.
    const { config } = buildConfig(SCENARIO.fields, initialValues(SCENARIO));
    expect(config.grid).toEqual({
      demand: [0.8, 1.0, 1.2, 1.5, 1.8, 2.1],
      capacity: [0.9, 1.0, 1.1, 1.2],
      unit_cost: [0.9, 1.0, 1.1],
    });
    // ...and not as a literal flat key, which the model never looks for.
    expect(Object.keys(config)).not.toContain("grid.demand");
  });

  it("parses number lists from commas or whitespace, and omits an empty one", () => {
    const fields: ConfigField[] = [{ key: "series", label: "s", kind: "number-list" }];
    expect(buildConfig(fields, { series: "1, 2  3" }).config).toEqual({ series: [1, 2, 3] });
    expect(buildConfig(fields, { series: "" }).config).toEqual({});
    expect(buildConfig(fields, { series: "1, nope" }).errors).toHaveProperty("series");
  });

  it("rejects a fractional value for an int field", () => {
    const fields: ConfigField[] = [{ key: "days", label: "d", kind: "int", default: 14 }];
    expect(buildConfig(fields, { days: "1.5" }).errors).toHaveProperty("days");
    expect(buildConfig(fields, { days: "1.5" }).config).toEqual({});
    expect(buildConfig(fields, { days: "15" }).config).toEqual({ days: 15 });
  });

  it("keeps strings as strings, trimmed", () => {
    const fields: ConfigField[] = [{ key: "column", label: "c", kind: "string" }];
    expect(buildConfig(fields, { column: "  trips " }).config).toEqual({ column: "trips" });
  });
});

describe("initialValues", () => {
  it("leaves defaultless fields blank so they are omitted unless typed into", () => {
    const values = initialValues(GUROBI_SCHEDULING);
    expect(values.time_limit_s).toBe("");
    expect(values.staff_count).toBe("20");
    expect(values.use_sample_data).toBe(true);
  });
});
