import { Link } from "react-router";

export function NotFound({
  title = "Not found",
  detail,
}: {
  title?: string;
  detail?: string;
}) {
  return (
    <div className="mx-auto max-w-[52ch] py-20 text-center">
      <h1 className="m-0 text-[1.4rem]">{title}</h1>
      {detail !== undefined && (
        <p className="mt-3 text-[0.86rem] leading-relaxed text-dim">{detail}</p>
      )}
      <Link to="/" className="mt-6 inline-block text-[0.82rem] font-semibold text-accent">
        ← Back to the overview
      </Link>
    </div>
  );
}
