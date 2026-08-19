# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a finished campaign's own measurements say the next one should reserve.

A campaign records what every container actually used (``resource_usage``, written by an
auto-injected plugin for every campaign) and what its ``.vast`` declared. The gap between
the two is knowable, actionable and almost never looked at: reservations are written once
from a guess and then carried, unexamined, through every sweep that follows. On the cluster
that guess is paid on **every job** -- a campaign's throughput is ``quota // pod_request`` --
so a pod reserving twice what it needs halves the sweep for nothing.

This module turns that gap into advice. It is the AUTHORITY for the sizing rules: the same
numbers reach an agent through ``get_campaign_summary`` and a human through the web UI's
Details panel, and they must not disagree about what a campaign should reserve.

**The contract.** An advice item is self-describing::

    {"kind": "cpu_over_reserved",     # stable slug, for a consumer that wants to branch
     "severity": "suggestion",        # suggestion | warning
     "title": "...",                  # one line, always renderable as-is
     "detail": "...",                 # a sentence or two of why
     "evidence": {...}}               # the numbers behind it, for a consumer that draws

`title` and `detail` are plain text on purpose. A consumer that has never heard of a `kind`
can still show the item correctly, so a new kind added here appears everywhere without a
matching change in every reader -- which is what makes "everything the MCP suggests is
visible in the UI" a property of the design rather than a promise someone has to keep.

The two rules differ, and the difference is not cosmetic:

* **CPU is sized on sustained use** (p95) plus headroom. Exceeding a cpu reservation costs
  CFS throttling for that scheduling period -- slower, still correct -- which is the right
  price for not reserving a brief peak permanently.
* **Memory is sized on the PEAK** plus headroom. Exceeding a memory limit is an OOM kill:
  the run dies and the campaign loses a cell. Sizing memory on a percentile would be
  choosing how often a run survives.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from robovast.common.quantity import to_bytes, to_cores

#: Headroom over sustained CPU use. Absorbs the p95->peak gap.
CPU_HEADROOM = 1.25

#: CPU reservations are rounded up to quarter cores: 4.75 is legible in a diff and
#: reproduces exactly, while 4.7531... invites the reader to believe the measurement
#: resolved something it did not.
CPU_GRANULARITY = 0.25

#: Headroom over the memory peak.
MEM_HEADROOM = 1.25

#: Memory reservations are rounded up to 128 MiB. Kubernetes takes a byte count, but nobody
#: writes one: the value goes into a ``.vast`` as ``2Gi``.
MEM_GRANULARITY_BYTES = 128 * 1024 * 1024

#: Fewest 1 Hz samples a container needs before its percentiles mean anything. A campaign of
#: sub-second runs yields single digits of in-window ticks across every run put together, and
#: a p95 over seven points is the maximum wearing a percentile's name.
MIN_TICKS = 30

#: How far from the suggestion a declaration has to be before it is worth saying anything.
#: Reservations are guesses; flagging a 10% miss would train the reader to ignore the advice.
OVER_RESERVED_RATIO = 1.5
UNDER_RESERVED_RATIO = 1.0

#: What ``resource_usage`` calls the main container, regardless of what the ``.vast`` named
#: it. Mirrors ``common/log_tail.MAIN_CONTAINER``.
MEASURED_MAIN_CONTAINER = "robovast"

#: Per-container CPU and memory, pooled over every tick of every run.
#:
#: The inner query is load-bearing: one row of ``resource_usage`` is one PROCESS NAME, not a
#: container, so per-tick values must be summed before any max or percentile -- a tick is
#: concurrent demand, and the largest single process is not it.
USAGE_SQL = """
    SELECT container,
           PERCENTILE(cores, 95) AS cpu_p95, MAX(cores) AS cpu_peak,
           SUM(cores) AS core_seconds,
           MAX(bytes) AS mem_peak, COUNT(*) AS ticks
    FROM (SELECT container, config_name, run_id, timestamp,
                 SUM(cpu_percent) / 100.0 AS cores,
                 SUM(memory_rss_bytes) AS bytes
          FROM resource_usage WHERE in_window = 1
          GROUP BY container, config_name, run_id, timestamp)
    GROUP BY container
"""

#: The containers the campaign declared, and what it reserved for each. The bare container
#: rows matter most when there is no reservation row: a ``.vast`` need not declare
#: ``resources`` at all, and that is the campaign whose author most needs this -- but the
#: measured main container is recorded under the role name ``robovast``, so without the
#: declared names there is nothing to translate it into a name from their file.
DECLARED_SQL = """
    SELECT fullkey, value FROM config_view
    WHERE (fullkey LIKE '$.execution.containers.%'
           AND fullkey NOT LIKE '$.execution.containers.%.%')
       OR fullkey LIKE '$.execution.containers.%.resources.cpu'
       OR fullkey LIKE '$.execution.containers.%.resources.memory'
"""


def ceil_to(value: float, step: float) -> float:
    """Round *value* up to the next multiple of *step*, at least one step."""
    if not math.isfinite(value) or value <= 0:
        return step
    return max(step, math.ceil(value / step) * step)


def format_cores(cores: float) -> str:
    """Cores at the granularity they are reserved at -- the number to type into a ``.vast``."""
    rounded = round(cores, 2)
    return str(int(rounded)) if float(rounded).is_integer() else str(rounded)


def format_memory(num_bytes: float) -> str:
    """Bytes as the suffixed form a ``.vast`` is written in (``2Gi``, ``512Mi``)."""
    gib = 1024 ** 3
    if num_bytes > 0 and num_bytes % gib == 0:
        return f"{int(num_bytes // gib)}Gi"
    return f"{round(num_bytes / 1024 ** 2)}Mi"


def _container_of(fullkey: str, suffix: str = "") -> Optional[str]:
    prefix = "$.execution.containers."
    if not fullkey.startswith(prefix):
        return None
    rest = fullkey[len(prefix):]
    if suffix:
        if not rest.endswith(suffix):
            return None
        rest = rest[: -len(suffix)]
    return rest if rest and "." not in rest else None


def _declared(rows: list[dict]) -> tuple[set, dict, dict]:
    """``(container names, cpu by name, memory-bytes by name)`` from the config rows."""
    names, cpu, memory = set(), {}, {}
    for row in rows:
        key = row.get("fullkey")
        if not isinstance(key, str):
            continue
        bare = _container_of(key)
        if bare:
            names.add(bare)
            continue
        name = _container_of(key, ".resources.cpu")
        if name:
            names.add(name)
            cores = to_cores(row.get("value"))
            if cores is not None:
                cpu[name] = cores
            continue
        name = _container_of(key, ".resources.memory")
        if name:
            names.add(name)
            num_bytes = to_bytes(row.get("value"))
            if num_bytes is not None:
                memory[name] = num_bytes
    return names, cpu, memory


def _resolve_names(measured: list[str], declared_names: set) -> dict:
    """Measured container name -> the name the ``.vast`` calls it.

    Not a plain join: ``resource_usage`` records the MAIN container under the fixed role name
    ``robovast`` while the campaign declares it by its own name. Secondaries match exactly, so
    the main one takes whatever single declaration went unclaimed -- and where the leftovers
    are ambiguous it keeps its own name rather than guessing, since a wrong pairing compares
    one container's use against another's reservation.
    """
    out, unclaimed = {}, set(declared_names)
    for name in measured:
        if name in unclaimed:
            unclaimed.discard(name)
            out[name] = name
    for name in measured:
        if name in out:
            continue
        if name == MEASURED_MAIN_CONTAINER and len(unclaimed) == 1:
            out[name] = unclaimed.pop()
        else:
            out[name] = name
    return out


def resource_advice(usage_rows: list[dict], declared_rows: list[dict]) -> list[dict]:
    """Advice items for one campaign's reservations. Empty when there is nothing to say.

    Args:
        usage_rows: rows of :data:`USAGE_SQL`.
        declared_rows: rows of :data:`DECLARED_SQL`.
    """
    usable = [r for r in usage_rows if r.get("container")]
    if not usable:
        return []
    names, declared_cpu, declared_mem = _declared(declared_rows)
    resolved = _resolve_names([r["container"] for r in usable], names)

    containers, thin = [], []
    for row in usable:
        label = resolved.get(row["container"], row["container"])
        ticks = int(row.get("ticks") or 0)
        if ticks < MIN_TICKS:
            thin.append(label)
            continue
        cpu_p95 = float(row.get("cpu_p95") or 0.0)
        mem_peak = float(row.get("mem_peak") or 0.0)
        containers.append({
            "container": label,
            "cpu_p95": cpu_p95,
            "cpu_peak": float(row.get("cpu_peak") or 0.0),
            "cpu_suggested": ceil_to(cpu_p95 * CPU_HEADROOM, CPU_GRANULARITY),
            "cpu_declared": declared_cpu.get(label),
            "mem_peak": mem_peak,
            "mem_suggested": ceil_to(mem_peak * MEM_HEADROOM, MEM_GRANULARITY_BYTES),
            "mem_declared": declared_mem.get(label),
            "core_seconds": float(row.get("core_seconds") or 0.0),
            "ticks": ticks,
        })

    advice: list[dict] = []
    if thin and not containers:
        advice.append({
            "kind": "resources_unmeasurable",
            "severity": "suggestion",
            "title": "Too few samples to size this campaign's containers",
            "detail": (
                f"The resource monitor samples at 1 Hz and these runs are shorter than that, "
                f"so {', '.join(sorted(thin))} produced under {MIN_TICKS} samples in total. "
                "The measurement is real but a percentile over it is not; run longer trials, "
                "or size from a campaign whose runs last several seconds."),
            "evidence": {"containers": sorted(thin)},
        })
        return advice

    for what, declared_key, suggested_key, fmt, unit in (
        ("cpu", "cpu_declared", "cpu_suggested", format_cores, "cpu"),
        ("memory", "mem_declared", "mem_suggested", format_memory, "memory"),
    ):
        sized = list(containers)
        pod_declared = (sum(c[declared_key] for c in sized)
                        if sized and all(c[declared_key] is not None for c in sized) else None)
        pod_suggested = sum(c[suggested_key] for c in sized)
        per_container = {c["container"]: fmt(c[suggested_key]) for c in sized}

        if pod_declared is None:
            advice.append({
                "kind": f"{what}_not_declared",
                "severity": "suggestion",
                "title": (f"No {unit} reservation declared; measured use suggests "
                          f"{fmt(pod_suggested)} per pod"),
                "detail": (
                    f"Set execution.containers.<name>.resources.{unit} from what this campaign "
                    f"used: {', '.join(f'{k} {v}' for k, v in per_container.items())}. "
                    + _basis(what)),
                "evidence": {"suggested_pod": fmt(pod_suggested),
                             "suggested_per_container": per_container},
            })
            continue

        ratio = pod_declared / pod_suggested if pod_suggested else 1.0
        if ratio >= OVER_RESERVED_RATIO:
            advice.append({
                "kind": f"{what}_over_reserved",
                "severity": "suggestion",
                "title": (f"Reserves {fmt(pod_declared)} {unit} per pod and needs about "
                          f"{fmt(pod_suggested)}"),
                "detail": (
                    f"Reducing it would fit {ratio:.1f}x as many jobs in the same quota, on "
                    f"every job of every sweep. Per container: "
                    f"{', '.join(f'{k} {v}' for k, v in per_container.items())}. "
                    + _basis(what)),
                "evidence": {"declared_pod": fmt(pod_declared),
                             "suggested_pod": fmt(pod_suggested),
                             "suggested_per_container": per_container,
                             "throughput_factor": round(ratio, 2)},
            })
        elif pod_declared < pod_suggested * UNDER_RESERVED_RATIO:
            advice.append({
                "kind": f"{what}_under_reserved",
                "severity": "warning",
                "title": (f"Reserves {fmt(pod_declared)} {unit} per pod but used up to "
                          f"{fmt(pod_suggested)}"),
                "detail": (_UNDER_DETAIL[what] + " Per container: "
                           + ", ".join(f"{k} {v}" for k, v in per_container.items())),
                "evidence": {"declared_pod": fmt(pod_declared),
                             "suggested_pod": fmt(pod_suggested),
                             "suggested_per_container": per_container},
            })

    if thin:
        advice.append({
            "kind": "resources_partially_unmeasurable",
            "severity": "suggestion",
            "title": f"Not sized: {', '.join(sorted(thin))} (under {MIN_TICKS} samples)",
            "detail": ("These containers ran for less than the 1 Hz sampler can characterise, "
                       "so they are left out of the figures above rather than sized from a "
                       "handful of ticks."),
            "evidence": {"containers": sorted(thin)},
        })
    return advice


_UNDER_DETAIL = {
    "cpu": ("Exceeding a cpu reservation costs throttling for that scheduling period, so "
            "this is slow rather than broken -- but the runs are not getting the cpu the "
            "campaign says they get, which makes timings incomparable across lanes."),
    "memory": ("Exceeding a memory limit is an OOM kill, not throttling: a run that touches "
               "the limit dies and the campaign loses that cell."),
}


def _basis(what: str) -> str:
    if what == "cpu":
        return (f"Sized on sustained use (p95) plus {round((CPU_HEADROOM - 1) * 100)}% "
                "headroom, rounded up to a quarter core.")
    return (f"Sized on the PEAK plus {round((MEM_HEADROOM - 1) * 100)}% headroom, rounded up "
            "to 128Mi -- the peak and not a percentile, because exceeding a memory limit is "
            "an OOM kill rather than throttling.")


def campaign_advice(query_rows) -> dict[str, Any]:
    """Advice for a campaign, given a ``query_rows(sql) -> list[dict]`` callable.

    Returns ``{"advice": [...]}`` -- a key rather than a bare list so a caller can merge it
    into a larger summary, and so a future non-resource advice source has somewhere to land.
    """
    return {"advice": resource_advice(query_rows(USAGE_SQL), query_rows(DECLARED_SQL))}
