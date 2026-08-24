import { useEffect, useRef, useState } from "react";

/**
 * Copy-to-clipboard with a two-second acknowledgement.
 *
 * `navigator.clipboard` is undefined on insecure origins, which includes a
 * plain-HTTP dev server on anything but localhost. Silently doing nothing
 * would make the button look broken, so that case shows a failure mark.
 */
export function CopyButton({ value, label = "copy" }: { value: string; label?: string }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
  }, []);

  function flash(next: "done" | "failed") {
    setState(next);
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 2000);
  }

  return (
    <button
      type="button"
      title={`${label}: ${value}`}
      aria-label={`${label} ${value}`}
      className="cursor-pointer border-0 bg-transparent p-0 text-[0.75rem] leading-none text-faint hover:text-accent"
      onClick={() => {
        const clipboard = navigator.clipboard as Clipboard | undefined;
        if (!clipboard) {
          flash("failed");
          return;
        }
        clipboard.writeText(value).then(
          () => flash("done"),
          () => flash("failed"),
        );
      }}
    >
      {state === "done" ? "✓" : state === "failed" ? "✕" : "⧉"}
    </button>
  );
}
