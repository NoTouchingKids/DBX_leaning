/**
 * Tests for the review surface itself.
 *
 * The harness is the thing nine other people will trust to tell them whether
 * their view is broken, so the harness lying is worse than a view being
 * broken. Three properties are worth pinning: every lifecycle state really is
 * rendered, a throwing signature is contained rather than taking the page
 * down, and the reduced-motion toggle actually reaches the hook it claims to.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelView, ModelViewProps } from "@/components/models/contract";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import { auditHonesty, HARNESS_STATES } from "./harness";
import { ModelGallery } from "./ModelGallery";
import { restoreMatchMedia } from "./reducedMotionOverride";
import { ViewHarness } from "./ViewHarness";

const HONEST =
  "The cell grid is decorative: there is no per-cell data in the payload. Pacing is real — it tracks nodes_explored from the progress payload.";

function stubView(overrides: Partial<ModelView> = {}): ModelView {
  return {
    model: "gurobi_scheduling",
    honesty: HONEST,
    Signature: ({ state, snapshot }: ModelViewProps) => (
      <div data-testid="signature">
        sig:{state ?? "null"}:{snapshot.progress.length}
      </div>
    ),
    charts: [
      {
        id: "gap",
        title: "MIP gap",
        Chart: ({ snapshot }: ModelViewProps) => (
          <div data-testid="chart">chart:{snapshot.progress.length}</div>
        ),
      },
    ],
    ...overrides,
  };
}

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  // React logs every boundary-caught error; the boundary tests below are
  // supposed to produce them.
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  consoleError.mockRestore();
  restoreMatchMedia();
});

describe("ViewHarness", () => {
  it("renders the view once per lifecycle state, including null and STARTING", () => {
    render(<ViewHarness view={stubView()} fixture="typical" />);
    const signatures = screen.getAllByTestId("signature");
    expect(signatures).toHaveLength(HARNESS_STATES.length);
    expect(signatures.map((el) => el.textContent?.split(":")[1])).toEqual([
      "null",
      "STARTING",
      "QUEUED",
      "RUNNING",
      "SUCCEEDED",
      "FAILED",
      "CANCELLED",
      "INFEASIBLE",
    ]);
  });

  it("shows the honesty note beside every state, not once at the top", () => {
    render(<ViewHarness view={stubView()} fixture="typical" />);
    expect(screen.getAllByText(HONEST)).toHaveLength(HARNESS_STATES.length);
  });

  it("calls out a view with no honesty note", () => {
    render(<ViewHarness view={stubView({ honesty: "" })} fixture="typical" />);
    expect(screen.getAllByText(/No honesty note/i).length).toBeGreaterThan(0);
  });

  it("hands each state a genuinely different snapshot", () => {
    render(<ViewHarness view={stubView()} fixture="typical" />);
    const counts = screen.getAllByTestId("signature").map((el) => el.textContent?.split(":")[2]);
    // null / STARTING / QUEUED have no progress; RUNNING is partial;
    // SUCCEEDED is the whole run.
    expect(counts.slice(0, 3)).toEqual(["0", "0", "0"]);
    expect(Number(counts[3])).toBeGreaterThan(0);
    expect(Number(counts[4])).toBeGreaterThan(Number(counts[3]));
  });

  it("renders charts, and stops rendering them when asked", () => {
    const { rerender } = render(<ViewHarness view={stubView()} fixture="typical" />);
    expect(screen.getAllByTestId("chart")).toHaveLength(HARNESS_STATES.length);
    rerender(<ViewHarness view={stubView()} fixture="typical" showCharts={false} />);
    expect(screen.queryAllByTestId("chart")).toHaveLength(0);
  });

  it("contains a signature that throws, and still renders that state's chart", () => {
    // The degrade-gracefully case: a lazily-loaded Three.js scene that fails
    // must leave the data behind it standing.
    const exploding = stubView({
      Signature: () => {
        throw new Error("WebGL context creation failed");
      },
    });
    render(<ViewHarness view={exploding} fixture="typical" />);
    expect(screen.getAllByText(/WebGL context creation failed/)).toHaveLength(
      HARNESS_STATES.length,
    );
    expect(screen.getAllByTestId("chart")).toHaveLength(HARNESS_STATES.length);
  });

  it("contains a chart that throws without losing the signature", () => {
    const exploding = stubView({
      charts: [
        {
          id: "bad",
          title: "Broken",
          Chart: () => {
            throw new Error("cannot read domain of undefined");
          },
        },
      ],
    });
    render(<ViewHarness view={exploding} fixture="typical" />);
    expect(screen.getAllByTestId("signature")).toHaveLength(HARNESS_STATES.length);
    expect(screen.getAllByText(/cannot read domain/)).toHaveLength(HARNESS_STATES.length);
  });

  it("flags a view whose model has no fixture script", () => {
    render(<ViewHarness view={stubView({ model: "not_a_real_model" })} fixture="typical" />);
    expect(screen.getByText(/no fixture script/i)).toBeInTheDocument();
  });
});

describe("auditHonesty", () => {
  it("passes a note that names both the real and the decorative parts", () => {
    expect(auditHonesty(HONEST).level).toBe("ok");
  });

  it("fails an absent one", () => {
    expect(auditHonesty(undefined).level).toBe("missing");
    expect(auditHonesty("   ").level).toBe("missing");
  });

  it("flags a note that only says the animation looks nice", () => {
    expect(auditHonesty("A pretty grid of cells that lights up as the solver works.").level).toBe(
      "thin",
    );
  });
});

describe("ModelGallery", () => {
  it("renders every view it is handed and nothing it is not", () => {
    render(
      <ModelGallery
        views={[stubView(), stubView({ model: "mcmc", charts: [] })]}
      />,
    );
    expect(screen.getByRole("heading", { name: "gurobi_scheduling" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "mcmc" })).toBeInTheDocument();
  });

  it("says so rather than looking broken when handed no views", () => {
    render(<ModelGallery views={[]} />);
    expect(screen.getByText(/No views passed in/i)).toBeInTheDocument();
  });

  it("names views whose model is not in MODEL_SPECS", () => {
    render(<ModelGallery views={[stubView({ model: "gurobi_schedulng" })]} />);
    expect(screen.getByText(/not found in MODEL_SPECS/i)).toBeInTheDocument();
  });

  it("switching fixture changes the traffic the view is handed", async () => {
    const user = userEvent.setup();
    render(<ModelGallery views={[stubView()]} />);
    const before = screen.getAllByTestId("signature").map((el) => el.textContent);
    await user.click(screen.getByRole("button", { name: "empty" }));
    const after = screen.getAllByTestId("signature").map((el) => el.textContent);
    expect(after).not.toEqual(before);
    // `empty` means zero messages in every state, terminal ones included.
    expect(after.every((text) => text?.endsWith(":0"))).toBe(true);
  });

  it("guards the dense fixture behind a click when several views are shown", async () => {
    const user = userEvent.setup();
    render(<ModelGallery views={[stubView(), stubView({ model: "mcmc" })]} />);
    await user.click(screen.getByRole("button", { name: "dense" }));
    expect(screen.queryAllByTestId("signature")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: /render anyway/i }));
    expect(screen.getAllByTestId("signature").length).toBeGreaterThan(0);
  });

  it("the reduced-motion toggle reaches usePrefersReducedMotion", async () => {
    const user = userEvent.setup();
    const view = stubView({
      Signature: () => (
        <div data-testid="signature">{usePrefersReducedMotion() ? "reduced" : "full"}</div>
      ),
      charts: [],
    });
    render(<ModelGallery views={[view]} />);
    expect(screen.getAllByTestId("signature")[0]).toHaveTextContent("full");

    await user.click(screen.getByRole("button", { name: /reduced motion/i }));
    // Remounting under a new key is what makes this land — the hook reads
    // matchMedia once, at mount.
    for (const el of screen.getAllByTestId("signature")) {
      expect(el).toHaveTextContent("reduced");
    }

    await user.click(screen.getByRole("button", { name: /reduced motion/i }));
    expect(screen.getAllByTestId("signature")[0]).toHaveTextContent("full");
  });

  it("is honest in the UI about what the toggle does not cover", async () => {
    const user = userEvent.setup();
    render(<ModelGallery views={[stubView()]} />);
    await user.click(screen.getByRole("button", { name: /reduced motion/i }));
    // Asserted against the whole rendered text because the sentence is
    // deliberately broken up by <code> spans naming the exact hook and CSS
    // rule; matching per element would pin the markup rather than the claim.
    expect(screen.getByText(/half-simulated/i)).toBeInTheDocument();
    expect(document.body.textContent).toContain("Emulate CSS media feature");
    expect(document.body.textContent).toContain("usePrefersReducedMotion");
  });
});
