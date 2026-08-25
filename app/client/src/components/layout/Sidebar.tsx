/**
 * The persistent navigation rail.
 *
 * The model group is generated from `MODEL_SPECS` and nothing else — that
 * registry is being extended from five models to nine on another track, and
 * anything here that enumerated models by hand would silently stop listing
 * the new ones. That includes the icons: they are initials derived from each
 * label, not a per-model glyph table that would need a new entry per model.
 */

import { NavLink } from "react-router";

import { initials } from "@/lib/format";
import { MODEL_SPECS } from "@/lib/models";

function NavRow({
  to,
  primary,
  secondary,
  glyph,
  collapsed,
  end,
}: {
  to: string;
  primary: string;
  secondary: string;
  glyph: React.ReactNode;
  collapsed: boolean;
  end?: boolean;
}) {
  return (
    <NavLink
      to={to}
      end={end}
      title={collapsed ? primary : undefined}
      className={({ isActive }) =>
        // flex-none: this is a flex child of a scrolling column, and without
        // it the browser shrinks rows below their content height under
        // vertical pressure — which clipped every label once the model list
        // was long enough to overflow.
        `relative flex w-full flex-none items-center gap-2.5 rounded-lg px-2.5 py-2 ` +
        `text-left no-underline transition-colors duration-100 motion-reduce:transition-none ` +
        (isActive
          ? "bg-accent-soft text-accent font-medium"
          : "text-ink hover:bg-paper") +
        (collapsed ? " justify-center" : "")
      }
    >
      {({ isActive }) => (
        <>
          {/* The active marker is a bar on the rail, not a border round the
              row: it survives the row being collapsed to an icon, and it is
              the one thing the eye can find without reading. */}
          {isActive && (
            <span
              aria-hidden
              className="absolute inset-y-1.5 left-0 w-[3px] rounded-r-full bg-accent"
            />
          )}
          <span
            className={
              `flex h-[26px] w-[26px] flex-none items-center justify-center rounded-md ` +
              `border text-[0.6rem] font-bold ` +
              (isActive
                ? "border-accent/40 bg-raised text-accent"
                : "border-line bg-paper text-faint")
            }
          >
            {glyph}
          </span>
          {/*
            Width and opacity animate together so the label does not clip
            abruptly. `motion-reduce` kills the transition, never the collapse
            — the rail must still be collapsible without animation.
          */}
          <span
            className={
              `overflow-hidden text-[0.82rem] whitespace-nowrap transition-[opacity,width] ` +
              `duration-200 motion-reduce:transition-none ` +
              (collapsed ? "w-0 opacity-0" : "w-auto opacity-100")
            }
          >
            <span className="block font-medium">{primary}</span>
            <span
              className={
                "block text-[0.6875rem] " + (isActive ? "text-accent/70" : "text-faint")
              }
            >
              {secondary}
            </span>
          </span>
        </>
      )}
    </NavLink>
  );
}

function GroupLabel({ children, collapsed }: { children: string; collapsed: boolean }) {
  return (
    <div
      className={
        // flex-none for the same reason as the rows above: without it this
        // shrinks under vertical pressure and the label is clipped mid-cap.
        `flex-none overflow-hidden px-2.5 pt-5 pb-1.5 text-[0.6875rem] font-semibold ` +
        `tracking-[0.06em] text-faint uppercase ` +
        `transition-opacity duration-150 motion-reduce:transition-none ` +
        (collapsed ? "h-0 py-0 opacity-0" : "opacity-100")
      }
    >
      {children}
    </div>
  );
}

export function Sidebar({
  collapsed,
  onToggle,
}: {
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <aside className="sticky top-0 flex h-screen min-w-0 flex-col overflow-hidden border-r border-line bg-sidebar">
      <div className="flex flex-none items-center gap-2.5 border-b border-line px-3 py-4">
        <div className="flex h-[30px] w-[30px] flex-none items-center justify-center rounded-lg bg-accent text-[0.65rem] font-bold text-white">
          DB
        </div>
        <div
          className={
            `overflow-hidden whitespace-nowrap transition-opacity duration-200 ` +
            `motion-reduce:transition-none ` + (collapsed ? "w-0 opacity-0" : "opacity-100")
          }
        >
          <div className="text-[0.875rem] font-semibold">DBX_leaning</div>
          <div className="text-[0.6875rem] text-faint">modelling platform</div>
        </div>
        <button
          type="button"
          onClick={onToggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
          className={
            `ml-auto flex h-[26px] w-[26px] flex-none cursor-pointer items-center justify-center ` +
            `rounded-md border border-edge bg-raised text-dim transition-transform duration-300 ` +
            `hover:bg-accent-soft hover:text-accent motion-reduce:transition-none ` +
            (collapsed ? "rotate-180" : "")
          }
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7">
            <path d="M10 3l-5 5 5 5" />
          </svg>
        </button>
      </div>

      <nav className="flex flex-col gap-0.5 overflow-y-auto p-2">
        <GroupLabel collapsed={collapsed}>Reference</GroupLabel>
        <NavRow
          to="/"
          end
          collapsed={collapsed}
          primary="Overview"
          secondary="how this works"
          glyph="◧"
        />

        <GroupLabel collapsed={collapsed}>Runs</GroupLabel>
        <NavRow
          to="/runs"
          collapsed={collapsed}
          primary="Run history"
          secondary="all models"
          glyph="≡"
        />

        <GroupLabel collapsed={collapsed}>Models</GroupLabel>
        {MODEL_SPECS.map((spec) => (
          <NavRow
            key={spec.name}
            to={`/models/${spec.name}`}
            collapsed={collapsed}
            primary={spec.label}
            secondary={spec.name}
            glyph={initials(spec.label)}
          />
        ))}
      </nav>

      <div
        className={
          `mt-auto overflow-hidden border-t border-line px-4 py-3 text-[0.68rem] ` +
          `leading-relaxed text-dim transition-opacity duration-150 motion-reduce:transition-none ` +
          (collapsed ? "opacity-0" : "opacity-100")
        }
      >
        Free Edition ceiling: 5 concurrent job tasks, account-wide — across all
        models, not per model.
      </div>
    </aside>
  );
}
