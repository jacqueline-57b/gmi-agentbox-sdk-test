"""Latency summaries, so two chapters do not define a percentile differently.

Nearest-rank: with twenty samples P95 is the second slowest and P99 the
slowest. Spelled out because definitions differ and the answer matters when a
row reports a budget as met or missed.
"""

from __future__ import annotations

import math
from typing import Any, List


def percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarise(values: List[float]) -> Any:
    if not values:
        return "no samples"
    return {
        "n": len(values),
        "min_s": round(min(values), 3),
        "p50_s": round(percentile(values, 50), 3),
        "p95_s": round(percentile(values, 95), 3),
        "p99_s": round(percentile(values, 99), 3),
        "max_s": round(max(values), 3),
    }
