/**
 * "What's real in this view" — the note that keeps a model's signature
 * animation from being read as data.
 *
 * It is not decoration and must not be dropped: a moving picture beside a
 * running job is taken as a live readout unless something says otherwise, and
 * for several models the motion is a fixed cadence with no numeric meaning at
 * all.
 *
 * But it was ten lines of prose sitting under every signature, on the panel a
 * user looks at most, permanently. So it is a disclosure: the SUMMARY carries
 * the warning — the reader is told the visual needs qualifying before they
 * decide whether to read the qualification — and the body carries the detail
 * for whoever wants it. Closed by default, native `<details>`, so it is
 * keyboard reachable and findable by the browser's own in-page search.
 */
export function HonestyNote({ children }: { children: React.ReactNode }) {
  return (
    <details className="group mt-3">
      <summary
        className={
          `inline-flex cursor-pointer list-none items-center gap-1.5 rounded-md ` +
          `text-[0.75rem] font-medium text-dim hover:text-accent ` +
          `[&::-webkit-details-marker]:hidden`
        }
      >
        <span
          aria-hidden
          className={
            `flex h-4 w-4 items-center justify-center rounded-full border border-edge ` +
            `text-[0.6rem] leading-none font-bold group-hover:border-accent`
          }
        >
          ?
        </span>
        What&rsquo;s real in this view
        <span
          aria-hidden
          className="text-[0.6rem] transition-transform duration-150 group-open:rotate-90 motion-reduce:transition-none"
        >
          ▶
        </span>
      </summary>
      <p className="mt-2 max-w-[68ch] text-[0.75rem] leading-relaxed text-faint">{children}</p>
    </details>
  );
}
