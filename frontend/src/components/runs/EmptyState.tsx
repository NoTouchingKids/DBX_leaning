import type { EmptyStateCopy } from "./emptyState";

export function EmptyState({ copy }: { copy: EmptyStateCopy }) {
  return (
    <div className="rounded-[10px] border border-dashed border-edge bg-paper px-4 py-10 text-center">
      <b className="block text-[0.86rem] text-dim">{copy.title}</b>
      <p className="mx-auto mt-1 max-w-[62ch] text-[0.76rem] leading-relaxed text-faint">
        {copy.body}
      </p>
    </div>
  );
}
