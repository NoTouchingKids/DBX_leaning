/**
 * Two-column app grid: nav rail + routed content.
 *
 * The collapse animates `grid-template-columns` rather than the sidebar's own
 * width. Animating the child's width leaves the grid track at its old size
 * for the duration, so the main column jumps at the end instead of sliding;
 * animating the track moves both sides together.
 *
 * `prefers-reduced-motion` removes the transition and nothing else. The rail
 * still collapses — it is a layout control, not decoration, and taking it
 * away from someone who asked for less motion would be taking away a feature.
 */

import { useEffect, useState } from "react";
import { Outlet } from "react-router";

import { Sidebar } from "./Sidebar";

const STORAGE_KEY = "dbx.sidebar.collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    // Storage can throw outright in a partitioned or blocked context.
    return false;
  }
}

export function AppShell() {
  const [collapsed, setCollapsed] = useState(readCollapsed);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
    } catch {
      // A UI preference that cannot be remembered is not worth an error path.
      // (Note this is a preference, not data: message history goes to
      // IndexedDB, never here.)
    }
  }, [collapsed]);

  return (
    <div
      className="grid min-h-screen transition-[grid-template-columns] duration-300 ease-out motion-reduce:transition-none"
      style={{ gridTemplateColumns: collapsed ? "60px minmax(0,1fr)" : "248px minmax(0,1fr)" }}
    >
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((v) => !v)} />
      <main className="min-w-0 px-6 pt-8 pb-20">
        <div className="mx-auto max-w-[1180px]">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
