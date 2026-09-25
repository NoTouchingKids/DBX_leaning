"""Exercise the DEPLOYED app's HTTP surface, from here, against a real workspace.

This is not a unit test and it does not belong in `tests/`. It answers a
question `tests/` never can, which is the one this platform keeps getting
wrong: **does the thing behave on the platform.** Four of v4's bugs so
far were invisible locally, and the pattern every time was that nothing raised.

    uv run python scripts/smoke_deployed.py                    # read-only
    uv run python scripts/smoke_deployed.py --run heartbeat    # spends a task slot
    uv run python scripts/smoke_deployed.py --run heartbeat --cancel-after 5
    uv run python scripts/smoke_deployed.py --run heartbeat --replay
    uv run python scripts/smoke_deployed.py --run heartbeat --replay-at 8 --cancel-after 15

**`--replay-at` is the one that exercises replay.** `--replay` only asks for
a gap the stream actually showed, and it asks after the terminal status —
when the job has detached and a 409 is the expected answer. A healthy run
shows no gap, so `--replay` alone can pass without replay ever having been
called against a live job. `--replay-at` asks the job, mid-run, to resend
everything from seq 0 up to the newest seq seen live, and checks the answer is
complete and agrees with what arrived live — closed part files and the
in-flight buffer both, which is the half a files-only replay would miss.

**Read-only by default, deliberately.** `--run` triggers a real Databricks job:
it takes one of the account's five concurrent task slots, and on Free Edition
that ceiling is shared across everything. Nothing here deploys, and nothing here
writes to Unity Catalog.

Auth is the CLI's own OAuth token (`databricks auth token`), so this runs as
you. The Apps proxy rejects anything without one — which is the same 302-to-a-
login-page that `job/auth.py` exists to avoid, arriving on a different door.

What it measures is worth as much as what it asserts. `docs/spike-results.md`
lists three numbers that would change specific code and records none of them:
whether the ingress cuts a long stream and when, whether idle dies sooner than
active, and whether SSE arrives promptly or in held-and-released batches.
`--run` prints all three as a side effect of watching one run, so
`DBX_WS_PING_S` and `DBX_SSE_KEEPALIVE_S` can stop being guesses from community
reports.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any

import httpx

#: The platform answers this path itself and never forwards it to the app —
#: HTTP 200, `content-length: 0`, and no `content-type` at all, where every
#: other route on the same app returns one. Verified 2026-09-05 against a
#: deployed `dbx-leaning`: `/healthz`, `/healthz/` and `/healthz?x=1` are all
#: swallowed, while `/HEALTHZ` falls through to the SPA — so the interception is
#: an exact, case-sensitive path match by the Apps ingress, not something the
#: app does.
#:
#: It matters because `docs/v4-rewrite-plan.md` assigns `/healthz` the job of
#: reporting what jobs were discovered and from where, precisely so a misspelled
#: tag is not a mystery. In production that report cannot be read. Treated here
#: as a recorded platform fact rather than a failure, because it is one.
SHADOWED_BY_PLATFORM = "/healthz"

TIMEOUT = httpx.Timeout(30.0, read=300.0)


# --------------------------------------------------------------------------
# resolving where to talk to, and who as
# --------------------------------------------------------------------------


def _cli(*args: str) -> str:
    """Run the Databricks CLI and return stdout, or die with its stderr."""
    proc = subprocess.run(["databricks", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        sys.exit(f"`databricks {' '.join(args)}` failed:\n{proc.stderr.strip()}")
    return proc.stdout


def app_url(name: str) -> str:
    """The deployed app's public URL, asked of the workspace rather than
    configured here — the same reason the app finds its jobs by tag."""
    for app in json.loads(_cli("apps", "list", "-o", "json")):
        if app.get("name") == name:
            url = (app.get("url") or "").rstrip("/")
            if not url:
                sys.exit(f"app {name!r} exists but has no URL yet — is it deployed?")
            return url
    sys.exit(f"no app named {name!r} in this workspace")


def bearer() -> str:
    """An OAuth token for the CLI's current profile.

    Not printed, not written to the repo. It expires in an hour, which is
    longer than any run this script watches.
    """
    return json.loads(_cli("auth", "token"))["access_token"]


# --------------------------------------------------------------------------
# read-only probes
# --------------------------------------------------------------------------


def probe(client: httpx.Client, base: str) -> int:
    """GET the endpoints that cost nothing. Returns the failure count."""
    failures = 0

    print("== read-only ==")
    for path in ("/api/models", "/api/whoami", "/api/schema", "/"):
        started = time.monotonic()
        try:
            r = client.get(base + path)
        except httpx.HTTPError as exc:  # noqa: PERF203 - one report per path
            print(f"  {path:<14} ERROR {exc}")
            failures += 1
            continue
        ms = (time.monotonic() - started) * 1000
        ok = r.status_code == 200 and len(r.content) > 0
        failures += not ok
        note = "" if ok else "  <-- empty or non-200"
        print(f"  {path:<14} {r.status_code} {len(r.content):>6}B {ms:6.0f}ms{note}")

    # Recorded, never asserted: see SHADOWED_BY_PLATFORM.
    r = client.get(base + SHADOWED_BY_PLATFORM)
    shadowed = r.status_code == 200 and not r.content and "content-type" not in r.headers
    state = "shadowed by the Apps ingress (expected)" if shadowed else "reaches the app"
    print(f"  {SHADOWED_BY_PLATFORM:<14} {r.status_code} {len(r.content):>6}B  {state}")

    models = client.get(base + "/api/models").json()
    names = [m["name"] for m in models.get("models", [])]
    print(f"\n  triggerable: {names or '(none)'}  source={models.get('source')}")
    if detail := models.get("detail"):
        print(f"  detail: {detail}")
    if not names:
        # An empty list is a real, explainable state — not necessarily a fault.
        print("  NOTE: no jobs carry the `project: dbx-leaning` tag, or none is deployed.")

    return failures


# --------------------------------------------------------------------------
# one live run: trigger, stream, and time the ingress
# --------------------------------------------------------------------------


def replay_live(
    client: httpx.Client, base: str, run_id: str, live: dict[int, str], elapsed: float
) -> int:
    """Ask the live job to resend 0..newest-seen-seq; check it is whole.

    The durable log is gap-free from seq 0 by construction (the envelope spec),
    so the answer must be exactly that range — including `client_visible=false`
    logs the live stream never carried — and every seq that DID arrive live
    must come back with the same type.
    """
    hi = max(live)
    got = client.get(f"{base}/api/runs/{run_id}/replay", params={"from_seq": 0, "to_seq": hi})
    print(f"  [{elapsed:6.1f}s] REPLAY(0,{hi}) -> {got.status_code}")
    if got.status_code != 200:
        print(f"  FAIL: {got.text[:200]}")
        return 1
    back = {m["seq"]: m.get("type") for m in got.json().get("messages", [])}
    missing = sorted(set(range(hi + 1)) - set(back))
    disagree = sorted(s for s, kind in live.items() if s in back and back[s] != kind)
    print(
        f"  replay returned {len(back)} for {hi + 1} expected; "
        f"missing {len(missing)}, type mismatches {len(disagree)}"
    )
    if missing:
        print(f"  FAIL: replay is missing seqs {missing[:20]}")
    if disagree:
        print(f"  FAIL: replay disagrees with the live stream at seqs {disagree[:20]}")
    return int(bool(missing)) + int(bool(disagree))


def watch(
    client: httpx.Client,
    base: str,
    run_id: str,
    *,
    cancel_after: float | None,
    replay: bool,
    replay_at: float | None = None,
) -> int:
    """Stream a run to its terminal status, timing every gap along the way.

    The gap distribution is the point as much as the messages are: SSE that
    arrives in held-and-released batches is not live, and no amount of reading
    the code can tell you which one this ingress does.
    """
    failures = 0
    seqs: list[int] = []
    live: dict[int, str] = {}
    replayed = False
    kinds: dict[str, int] = {}
    gaps: list[float] = []
    terminal: dict[str, Any] | None = None
    cancelled_at: float | None = None

    opened = time.monotonic()
    last = opened
    first_byte: float | None = None

    url = f"{base}/api/runs/{run_id}/stream"
    print(f"\n== streaming {run_id} ==")
    with client.stream("GET", url, headers={"Accept": "text/event-stream"}) as r:
        if r.status_code != 200:
            print(f"  stream returned {r.status_code}")
            return 1
        event = None
        for line in r.iter_lines():
            now = time.monotonic()
            if first_byte is None:
                first_byte = now - opened
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue  # keepalive comment, or the blank line ending an event
            gaps.append(now - last)
            last = now
            msg = json.loads(line.split(":", 1)[1].strip())
            seqs.append(msg["seq"])
            live[msg["seq"]] = msg.get("type", "?")
            kinds[event or msg.get("type", "?")] = kinds.get(event or msg.get("type", "?"), 0) + 1

            if msg.get("type") == "status":
                print(
                    f"  [{now - opened:6.1f}s] seq={msg['seq']:<4} {msg['status']}"
                    f" terminal={msg.get('terminal')}"
                )
                if msg.get("terminal"):
                    terminal = msg
                    break

            if replay_at is not None and not replayed and now - opened >= replay_at:
                replayed = True
                failures += replay_live(client, base, run_id, live, now - opened)

            if cancel_after is not None and cancelled_at is None and now - opened >= cancel_after:
                cancelled_at = now
                ack = client.post(f"{base}/api/runs/{run_id}/cancel")
                print(f"  [{now - opened:6.1f}s] CANCEL -> {ack.status_code} {ack.text[:200]}")
                if ack.status_code != 200:
                    failures += 1

    elapsed = time.monotonic() - opened

    # --- what the stream actually delivered -------------------------------
    print(f"\n  messages={len(seqs)} kinds={kinds} elapsed={elapsed:.1f}s")
    if first_byte is not None:
        print(f"  first byte: {first_byte * 1000:.0f}ms")
    if gaps:
        ordered = sorted(gaps)
        p50 = ordered[len(ordered) // 2]
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        biggest = max(gaps)
        print(
            f"  inter-event gap: p50={p50 * 1000:.0f}ms p99={p99 * 1000:.0f}ms "
            f"max={biggest * 1000:.0f}ms"
        )
        # Held-and-released delivery shows up as a p50 near zero with a large
        # max — many messages arriving at once after a long wait.
        if p50 < 0.02 and biggest > 2.0:
            print(
                "  WARNING: looks BUFFERED — many messages arriving together after a"
                " long wait. `X-Accel-Buffering: no` may not be honoured here."
            )
        else:
            print("  delivery looks prompt (not held-and-released)")

    if terminal is None:
        failures += 1
        print(
            "  FAIL: stream ended without a terminal status — the ingress may have"
            f" cut it at ~{elapsed:.0f}s. That is one of the three numbers"
            " docs/spike-results.md is missing; record it."
        )

    # --- seq integrity ----------------------------------------------------
    if seqs:
        missing = sorted(set(range(min(seqs), max(seqs) + 1)) - set(seqs))
        print(f"  seq {min(seqs)}..{max(seqs)}, missing {len(missing)}")
        if missing and replay:
            lo, hi = missing[0], missing[-1]
            got = client.get(
                f"{base}/api/runs/{run_id}/replay", params={"from_seq": lo, "to_seq": hi}
            )
            print(f"  replay({lo},{hi}) -> {got.status_code}")
            if got.status_code == 200:
                back = got.json().get("messages", [])
                filled = {m["seq"] for m in back}
                still = sorted(set(missing) - filled)
                print(f"  replay returned {len(back)}; still missing {len(still)}")
                failures += bool(still)
            else:
                # 409 once the job has detached is EXPECTED for a finished run:
                # the app holds no grant on the telemetry volume, so there is no
                # other live backfill path. See docs/v4-rewrite-plan.md.
                print(f"  {got.text[:200]}")
                failures += got.status_code not in (409,)
        elif missing:
            print("  (gaps present; pass --replay to try filling them)")

    if replay_at is not None and not replayed:
        failures += 1
        print(
            f"  FAIL: the run ended before --replay-at {replay_at}s; replay was never"
            " exercised. Pass a longer run (e.g. --config '{\"seconds\": 60}')."
        )

    if cancelled_at is not None and terminal is not None:
        print(
            f"  cancel -> terminal took {last - cancelled_at:.2f}s"
            f" (status {terminal.get('status')})"
        )

    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--app", default="dbx-leaning", help="the Databricks app's name")
    ap.add_argument(
        "--run", metavar="MODEL", help="trigger this model — SPENDS one of five task slots"
    )
    ap.add_argument("--config", default="{}", help="DBX_MODEL_CONFIG, as JSON")
    ap.add_argument("--run-id", help="supply one to make the trigger idempotent")
    ap.add_argument(
        "--cancel-after",
        type=float,
        metavar="SECONDS",
        help="cancel the run this many seconds in, and time the ack",
    )
    ap.add_argument(
        "--replay", action="store_true", help="ask the job to resend any gap the stream showed"
    )
    ap.add_argument(
        "--replay-at",
        type=float,
        metavar="SECONDS",
        help="mid-run, ask the LIVE job to resend seq 0..newest and check it",
    )
    args = ap.parse_args()

    base = app_url(args.app)
    print(f"app: {base}")

    headers = {"Authorization": f"Bearer {bearer()}"}
    with httpx.Client(headers=headers, timeout=TIMEOUT, follow_redirects=False) as client:
        failures = probe(client, base)

        if args.run:
            try:
                config = json.loads(args.config)
            except json.JSONDecodeError as exc:
                sys.exit(f"--config is not JSON: {exc}")

            body: dict[str, Any] = {"model": args.run, "config": config}
            if args.run_id:
                body["run_id"] = args.run_id
            started = client.post(f"{base}/api/runs", json=body)
            print(f"\n== trigger {args.run} -> {started.status_code} ==")
            print(f"  {started.text[:400]}")
            if started.status_code != 202:
                return failures + 1
            run_id = started.json()["run_id"]
            failures += watch(
                client,
                base,
                run_id,
                cancel_after=args.cancel_after,
                replay=args.replay,
                replay_at=args.replay_at,
            )

    print(f"\n{'FAILURES: ' + str(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
