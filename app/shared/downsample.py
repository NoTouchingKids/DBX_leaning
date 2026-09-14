"""LTTB downsampling for result previews.

Largest-Triangle-Three-Buckets, not stride sampling. Stride sampling drops
whatever falls between strides, which reliably hides spikes exactly where
they matter (a forecast error blow-up lands between two kept points and
vanishes). LTTB keeps the points that carry the shape.

Pure Python and dependency-free on purpose: ``shared`` is imported by both
deployment artefacts, so it does not get to pull in numpy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["lttb", "downsample_rows"]


def lttb(points: Sequence[tuple[float, float]], threshold: int) -> list[tuple[float, float]]:
    """Downsample ``(x, y)`` points to at most ``threshold``, shape-preserving.

    First and last points are always kept — a chart's endpoints are not
    negotiable.
    """
    n = len(points)
    if threshold >= n or threshold <= 2:
        return list(points) if threshold >= n else _endpoints(points, threshold)

    every = (n - 2) / (threshold - 2)
    sampled: list[tuple[float, float]] = [points[0]]
    a = 0

    for i in range(threshold - 2):
        # Average of the *next* bucket forms the third triangle vertex.
        start = int((i + 1) * every) + 1
        end = min(int((i + 2) * every) + 1, n)
        if start >= end:
            start, end = min(start, n - 1), min(start + 1, n)
        bucket = points[start:end] or [points[-1]]
        avg_x = sum(p[0] for p in bucket) / len(bucket)
        avg_y = sum(p[1] for p in bucket) / len(bucket)

        # Pick the point in *this* bucket forming the largest triangle with
        # the previously kept point and that average.
        this_start = int(i * every) + 1
        this_end = min(int((i + 1) * every) + 1, n)
        ax, ay = points[a]
        best, best_area = this_start, -1.0
        for j in range(this_start, max(this_end, this_start + 1)):
            px, py = points[min(j, n - 1)]
            area = abs((ax - avg_x) * (py - ay) - (ax - px) * (avg_y - ay)) * 0.5
            if area > best_area:
                best, best_area = j, area
        a = min(best, n - 1)
        sampled.append(points[a])

    sampled.append(points[-1])
    return sampled


def _endpoints(points: Sequence[tuple[float, float]], threshold: int) -> list[tuple[float, float]]:
    if not points:
        return []
    if threshold <= 1:
        return [points[0]]
    return [points[0], points[-1]]


def downsample_rows(
    rows: Sequence[dict[str, Any]],
    threshold: int,
    *,
    x: str | None = None,
    y: str | None = None,
) -> list[dict[str, Any]]:
    """Downsample dict rows for a ``result.preview``.

    With ``x``/``y`` naming numeric columns, uses LTTB on that series and
    returns the *whole original rows* for the points it keeps — the preview
    stays as rich as the source, just shorter.

    Without them (or when the named columns aren't numeric) the rows aren't
    time-series-shaped, so there is no shape to preserve: falls back to an
    evenly spaced sample that still keeps the first and last row.
    """
    n = len(rows)
    if n <= threshold:
        return list(rows)
    if threshold <= 0:
        return []

    if x and y:
        try:
            series = [(float(r[x]), float(r[y])) for r in rows]
        except (KeyError, TypeError, ValueError):
            series = None
        if series is not None:
            index_of: dict[tuple[float, float], int] = {}
            for i, p in enumerate(series):
                index_of.setdefault(p, i)
            kept = lttb(series, threshold)
            seen: set[int] = set()
            out: list[dict[str, Any]] = []
            for p in kept:
                i = index_of.get(p)
                if i is not None and i not in seen:
                    seen.add(i)
                    out.append(rows[i])
            return out

    if threshold == 1:
        return [rows[0]]
    step = (n - 1) / (threshold - 1)
    picked = sorted({min(int(round(i * step)), n - 1) for i in range(threshold)})
    return [rows[i] for i in picked]
