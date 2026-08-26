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

from robovast.common.config import DEFAULT_SHM_SIZE
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

#: Fraction of a container's CPU enforcement periods that may be throttled before it is worth
#: reporting. Not zero: a handful of throttled periods during bring-up is normal and saying so
#: every time would train a reader to ignore the finding. One percent of a two-minute run is
#: about a second of withheld CPU, which is where it stops being noise.
THROTTLE_WARN_RATIO = 0.01

#: How far from the suggestion a declaration has to be before it is worth saying anything.
#: Reservations are guesses; flagging a 10% miss would train the reader to ignore the advice.
OVER_RESERVED_RATIO = 1.5
UNDER_RESERVED_RATIO = 1.0

#: What ``resource_usage`` calls the main container, regardless of what the ``.vast`` named
#: it. Mirrors ``common/log_tail.MAIN_CONTAINER``.
MEASURED_MAIN_CONTAINER = "robovast"

#: Headroom over the shared-memory peak. Same rule as memory, and for a sharper version of
#: the same reason: overrunning ``/dev/shm`` is a SIGBUS, and one that arrives without even
#: the "OOMKilled" label to explain it.
SHM_HEADROOM = 1.25

#: Below this, nothing is said about ``/dev/shm`` at all.
#:
#: Shared memory is not always used. A single-container run, a run whose middleware is not
#: DDS, and one whose nodes are co-located in a process all touch almost none of it -- and
#: Fast DDS falls back to UDP where shared memory is unavailable. Measuring such a campaign
#: is right (a peak of nearly nothing is a real answer about the experiment), but advising it
#: about a size is not.
#:
#: The threshold is the LOCAL lane's own default rather than a number chosen here: a peak that
#: fits in what the smallest lane hands out for free needs no declaration to survive anywhere,
#: and reducing a declaration below it would buy nothing. One condition, and it covers every
#: shape above without naming any of them.
SHM_ADVICE_FLOOR_BYTES = 64 * 1024 * 1024

#: What a campaign composed today reserves when its ``.vast`` says nothing, in bytes.
#:
#: Derived from the config model rather than restated, so this cannot quote a size that is
#: no longer the one campaigns actually get.
DEFAULT_SHM_SIZE_BYTES = to_bytes(DEFAULT_SHM_SIZE)

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

#: The run's shared-memory pool: the highest any run peaked at, and the limit that was in
#: force. From ``runs`` rather than from the per-tick table because that is where the builder
#: puts the high-water mark -- and because an older campaign then yields NULLs instead of a
#: missing table, which is a value this module can reason about rather than an error.
#:
#: Not filtered to the trial window, unlike :data:`USAGE_SQL`: a participant allocates its
#: segments while it starts up, and a SIGBUS during bring-up loses the run just as completely.
SHM_SQL = """
    SELECT MAX(shm_peak_bytes) AS shm_peak, MAX(shm_limit_bytes) AS shm_limit
    FROM runs
"""

#: Container memory as the KERNEL accounts it, which is what the limit is enforced against.
#:
#: :data:`USAGE_SQL`'s ``mem_peak`` cannot answer this and is not a close-enough approximation.
#: One ``resource_usage`` row is a process, and RSS counts a shared page once per process, so
#: summing a tick over a stack of forty ROS nodes -- sharing libraries, and a Fast DDS
#: shared-memory segment mapped into each of them -- multiplies what the container actually
#: holds. Measured on one basic_nav campaign: summed RSS peaked at 5147 MiB in a container
#: running comfortably inside a 2944 MiB limit, whose largest single process held 1014 MiB.
#: Sizing from that would have told its author to reserve 2.3x what the run needed, on every
#: job of every sweep.
#:
#: Absent for a campaign recorded before the probe existed, and on any runtime without cgroup
#: v2 -- hence a missing table or an empty result, both of which the caller falls back from
#: rather than treating as zero.
SYSTEM_MEM_SQL = """
    SELECT container, MAX(memory_peak) AS mem_peak
    FROM system_usage WHERE in_window = 1 AND memory_peak IS NOT NULL
    GROUP BY container
"""

#: How much of each container's CPU budget the kernel actually withheld, per run.
#:
#: The per-run grouping is load-bearing and easy to get wrong: these are monotonic counters on
#: a cgroup, and every run is a fresh container, so a delta taken across runs is not a number
#: at all. Take it inside the run, then pool.
#:
#: ``nr_periods`` is the denominator because a throttle count means nothing without it -- two
#: throttled periods out of two thousand is noise from bring-up, and the same two out of twenty
#: is a container being held back continuously.
THROTTLE_SQL = """
    WITH per_run AS (
        SELECT config_name, run_id, container,
               MAX(nr_periods) - MIN(nr_periods) AS periods,
               MAX(nr_throttled) - MIN(nr_throttled) AS throttled
        FROM system_usage
        WHERE in_window = 1 AND nr_periods IS NOT NULL
        GROUP BY config_name, run_id, container)
    SELECT container,
           SUM(periods) AS periods,
           SUM(throttled) AS throttled,
           COUNT(*) AS runs,
           SUM(CASE WHEN throttled > 0 THEN 1 ELSE 0 END) AS runs_throttled
    FROM per_run GROUP BY container
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
       OR fullkey = '$.execution.shm_size'
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


def _declared(rows: list[dict]) -> tuple[set, dict, dict, Optional[float]]:
    """``(container names, cpu by name, memory-bytes by name, shm bytes)`` from the config.

    ``shm`` is ``execution.shm_size``, not a container's own reservation, because
    ``/dev/shm`` is one tmpfs shared by every container of the pod -- see
    :func:`resource_advice` for why a memory suggestion has to know about it.
    """
    names, cpu, memory, shm = set(), {}, {}, None
    for row in rows:
        key = row.get("fullkey")
        if not isinstance(key, str):
            continue
        if key == "$.execution.shm_size":
            shm = to_bytes(row.get("value"))
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
    return names, cpu, memory, shm


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


def resource_advice(usage_rows: list[dict], declared_rows: list[dict],
                    system_mem_rows: "list[dict] | None" = None) -> list[dict]:
    """Advice items for one campaign's reservations. Empty when there is nothing to say.

    Args:
        usage_rows: rows of :data:`USAGE_SQL`.
        declared_rows: rows of :data:`DECLARED_SQL`.
        system_mem_rows: rows of :data:`SYSTEM_MEM_SQL`, when the campaign recorded them.
            Where a container appears here its kernel-accounted peak REPLACES the summed-RSS
            figure, which over-reports (see :data:`SYSTEM_MEM_SQL`). Falling back rather than
            refusing keeps this useful on campaigns recorded before the probe existed, but the
            two are not interchangeable and the advice says which one it used.
    """
    usable = [r for r in usage_rows if r.get("container")]
    if not usable:
        return []
    names, declared_cpu, declared_mem, declared_shm = _declared(declared_rows)
    resolved = _resolve_names([r["container"] for r in usable], names)
    system_mem = {r["container"]: r["mem_peak"]
                  for r in (system_mem_rows or []) if r.get("mem_peak") is not None}
    mem_source = "the kernel's own accounting for the container" if system_mem else (
        "the sum of its processes' RSS, which over-reports shared pages -- no cgroup "
        "memory figures were recorded for this campaign")

    containers, thin = [], []
    for row in usable:
        label = resolved.get(row["container"], row["container"])
        ticks = int(row.get("ticks") or 0)
        if ticks < MIN_TICKS:
            thin.append(label)
            continue
        cpu_p95 = float(row.get("cpu_p95") or 0.0)
        cgroup_peak = system_mem.get(row["container"])
        mem_peak = float(cgroup_peak if cgroup_peak is not None
                         else (row.get("mem_peak") or 0.0))
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

        # ``/dev/shm`` is a memory-backed emptyDir shared by every container, so its pages
        # are charged to the POD. Sizing memory from process RSS alone would advise a total
        # the tmpfs by itself could fill -- and overrunning shared memory kills a container
        # with SIGBUS (exit 135) rather than a clean OOM, so the death arrives with no
        # reason attached. Only memory is affected: there is no shared CPU allowance.
        shm_note = ""
        if what == "memory" and declared_shm:
            pod_suggested += declared_shm
            shm_note = (
                f" The per-container figures are process memory only; execution.shm_size "
                f"({format_memory(declared_shm)}) is a tmpfs charged to the pod, so the "
                f"limits have to total {fmt(pod_suggested)} with it added on top.")

        if pod_declared is None:
            advice.append({
                "kind": f"{what}_not_declared",
                "severity": "suggestion",
                "title": (f"No {unit} reservation declared; measured use suggests "
                          f"{fmt(pod_suggested)} per pod"),
                "detail": (
                    f"Set execution.containers.<name>.resources.{unit} from what this campaign "
                    f"used: {', '.join(f'{k} {v}' for k, v in per_container.items())}. "
                    + _basis(what, mem_source if what == "memory" else "") + shm_note),
                "evidence": {"suggested_pod": fmt(pod_suggested),
                             "suggested_per_container": per_container,
                             **({"shm_size": format_memory(declared_shm)}
                                if shm_note else {})},
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
                    + _basis(what, mem_source if what == "memory" else "") + shm_note),
                "evidence": {"declared_pod": fmt(pod_declared),
                             "suggested_pod": fmt(pod_suggested),
                             "suggested_per_container": per_container,
                             "throughput_factor": round(ratio, 2),
                             **({"shm_size": format_memory(declared_shm)}
                                if shm_note else {})},
            })
        elif pod_declared < pod_suggested * UNDER_RESERVED_RATIO:
            advice.append({
                "kind": f"{what}_under_reserved",
                "severity": "warning",
                "title": (f"Reserves {fmt(pod_declared)} {unit} per pod but used up to "
                          f"{fmt(pod_suggested)}"),
                "detail": (_UNDER_DETAIL[what] + " Per container: "
                           + ", ".join(f"{k} {v}" for k, v in per_container.items())
                           + shm_note),
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


def _basis(what: str, mem_source: str = "") -> str:
    """The one sentence saying how a suggestion was arrived at.

    *mem_source* names WHICH measurement the memory peak came from. It is stated rather than
    assumed because the two available answers differ by more than a rounding: the kernel's own
    accounting is what the limit is enforced against, while a sum of per-process RSS counts
    shared pages once per process and can over-report several-fold on a stack of many nodes.
    A reader deciding whether to trust a number needs to know which one they were handed.
    """
    if what == "cpu":
        return (f"Sized on sustained use (p95) plus {round((CPU_HEADROOM - 1) * 100)}% "
                "headroom, rounded up to a quarter core.")
    basis = (f"Sized on the PEAK plus {round((MEM_HEADROOM - 1) * 100)}% headroom, rounded up "
             "to 128Mi -- the peak and not a percentile, because exceeding a memory limit is "
             "an OOM kill rather than throttling.")
    return f"{basis} Measured from {mem_source}." if mem_source else basis


def shm_advice(shm_rows: list[dict], declared_rows: list[dict]) -> list[dict]:
    """Advice about ``execution.shm_size``. Empty unless the campaign actually uses the pool.

    Silence is the normal answer and the most important behaviour here. Nothing is said when
    the pool was not measured, and nothing is said when the peak fits inside
    :data:`SHM_ADVICE_FLOOR_BYTES` -- which is the case for every campaign that does not talk
    over shared memory at all. Advising those would be advice derived from the absence of a
    measurement, and it would train the reader to ignore the items that do mean something.

    Args:
        shm_rows: rows of :data:`SHM_SQL`.
        declared_rows: rows of :data:`DECLARED_SQL`.
    """
    row = (shm_rows or [{}])[0]
    peak = row.get("shm_peak")
    if peak is None:
        # Unmeasured: a campaign from before the monitor sampled the pool, or a runtime with
        # no /dev/shm. NOT "used none" -- so nothing is claimed about it either way.
        return []
    peak = int(peak)
    if peak <= SHM_ADVICE_FLOOR_BYTES:
        return []

    limit = row.get("shm_limit")
    _, _, _, declared = _declared(declared_rows)
    suggested = ceil_to(peak * SHM_HEADROOM, MEM_GRANULARITY_BYTES)
    evidence: dict[str, Any] = {"peak": format_memory(peak),
                                "suggested": format_memory(suggested)}
    if limit is not None:
        # The tmpfs's own size, as the run saw it. Carried on every item because it answers
        # "did my declaration take effect?" -- a declared value the mount never got is
        # otherwise indistinguishable from one that was honoured.
        evidence["observed_limit"] = format_memory(int(limit))
    if declared:
        evidence["declared"] = format_memory(declared)

    if not declared:
        # Only a campaign recorded before robovast defaulted the pool can reach this: the
        # default is written into the campaign's config, so a composed campaign always has
        # a size to report. Kept rather than deleted because the advice is still true of
        # those runs -- they really were handed whichever lane default applied -- and
        # dropping it would silently stop explaining their SIGBUS deaths.
        return [{
            "kind": "shm_not_declared",
            "severity": "warning",
            "title": (f"Uses up to {format_memory(peak)} of shared memory and ran before "
                      f"execution.shm_size had a default; a rerun reserves "
                      f"{format_memory(DEFAULT_SHM_SIZE_BYTES)} unless it says otherwise"),
            "detail": (
                "This campaign was given whichever default its lane happened to apply -- on "
                "the cluster the pod's memory limits, or the whole node when none were "
                f"declared; locally Docker's {format_memory(SHM_ADVICE_FLOOR_BYTES)}. Those "
                "disagree, which is why the same .vast could survive on one lane and die of "
                "SIGBUS (exit 135) on the other, unreported as an out-of-memory kill. A "
                "campaign composed today gets one size on both lanes; declare "
                f"execution.shm_size: {format_memory(suggested)} if this peak is "
                "representative. " + _SHM_BASIS),
            "evidence": evidence,
        }]

    if declared < suggested:
        return [{
            "kind": "shm_under_reserved",
            "severity": "warning",
            "title": (f"execution.shm_size is {format_memory(declared)} and shared memory "
                      f"peaked at {format_memory(peak)}"),
            "detail": (
                "A container that overruns the pool is killed with SIGBUS (exit 135) rather "
                "than a clean OOM, so the run dies with no reason attached to it. "
                f"Raise it to {format_memory(suggested)}. " + _SHM_BASIS),
            "evidence": evidence,
        }]

    ratio = declared / suggested
    if ratio >= OVER_RESERVED_RATIO:
        return [{
            "kind": "shm_over_reserved",
            "severity": "suggestion",
            "title": (f"execution.shm_size is {format_memory(declared)} and "
                      f"{format_memory(suggested)} would cover the peak"),
            "detail": (
                "The pool is a tmpfs charged to the pod, so its declared size is added to "
                "what the containers' memory limits have to total -- on the cluster that is "
                "paid on every job of every sweep. Lowering it reserves less for the same "
                "headroom. " + _SHM_BASIS),
            "evidence": {**evidence, "throughput_factor": round(ratio, 2)},
        }]
    return []


#: Why the suggested size is what it is. Its own constant because all three items end with it.
_SHM_BASIS = (f"Sized on the PEAK plus {round((SHM_HEADROOM - 1) * 100)}% headroom, rounded up "
              "to 128Mi, over every tick of every run including bring-up -- a participant "
              "allocates its segments as it starts, and a SIGBUS there loses the run too.")


def throttle_advice(throttle_rows: list[dict], declared_rows: list[dict]) -> list[dict]:
    """Whether the system under test was denied CPU it asked for. Empty when it was not.

    **A screen, not a verdict, and the valuable half is the silence.** Throttling says the
    allocation was *binding*; it does not say the stack misbehaved. A planner with slack can
    lose spikes and still meet every deadline that matters. So:

    * **Nothing reported** is a strong negative: no run in this campaign was starved, and a
      failure can be attributed to the stack rather than to the cluster. That is the result
      worth having, and it is why this stays quiet in the normal case.
    * **Something reported** is inconclusive alone. It marks the runs where a resource
      explanation is *available*, and hands the question to the stack's own health signals --
      controller frequency, missed control loops, planning failures -- which are what actually
      say whether it worked. This module cannot know those; they are per-stack.

    **Only the SUT is reported.** A campaign asks whether the stack under test behaves as
    expected, and nothing else in the results separates "nav2 failed" from "nav2 was cut off
    mid-plan" -- the run simply fails, plausibly, and is counted against the software. Measured
    on 2026-08-26: a nav2 container capped at 1.75 cores sat at its ceiling for 44% of ticks
    and lost 11 runs of 50 to late transforms, while every other signal, realtime factor
    included, looked healthy.

    The simulator and scenario are deliberately NOT reported here even when throttled harder.
    They are not under test, they are expected to burst and be clipped, and the question of
    whether their squeezing hurt is already answered by the realtime factor recorded per run.
    Reporting them would train a reader to skim past the one line that matters.

    Args:
        throttle_rows: rows of :data:`THROTTLE_SQL`. Empty for a campaign recorded before the
            probe existed, or on a host without cgroup v2 -- which is silence, not a pass.
        declared_rows: rows of :data:`DECLARED_SQL`, to name the container as its author does.
    """
    from robovast.common.config import SUT_CONTAINER  # noqa: PLC0415 - avoids a config import

    for row in throttle_rows or []:
        if row.get("container") != SUT_CONTAINER:
            continue
        periods = int(row.get("periods") or 0)
        throttled = int(row.get("throttled") or 0)
        if periods <= 0 or throttled <= 0:
            return []
        ratio = throttled / periods
        if ratio < THROTTLE_WARN_RATIO:
            return []
        runs = int(row.get("runs") or 0)
        runs_throttled = int(row.get("runs_throttled") or 0)
        # No name resolution here, unlike resource_advice: the SUT is measured under the same
        # name it is declared with. Only the MAIN container has the mismatch that needs
        # translating (declared as its own name, measured as the role name 'robovast').
        _, declared_cpu, _, _ = _declared(declared_rows)
        label = SUT_CONTAINER
        reserved = declared_cpu.get(label)
        return [{
            "kind": "sut_throttled",
            "severity": "warning",
            "title": (f"The system under test was denied CPU in {ratio * 100:.1f}% of "
                      f"enforcement periods"),
            "detail": (
                f"{runs_throttled} of {runs} run(s) had their '{label}' container held at its "
                f"CPU limit"
                + (f" ({format_cores(reserved)})" if reserved is not None else "")
                + ". This does not by itself mean the stack misbehaved -- it means a "
                "resource explanation is available for anything that went wrong in those "
                "runs, and nothing else in the results separates the two. Check the stack's "
                "own health signals there (controller frequency, missed control loops, "
                "planning failures); if they are clean, the throttling cost nothing and the "
                "allocation can stay. If they are not, raise "
                f"execution.containers.{label}.resources.cpu -- sizing on sustained use is not "
                "enough, because the limit is a ceiling and a planner's peaks are the work. "
                "Which runs: SELECT config_name, "
                "run_id FROM system_usage WHERE container = '" + SUT_CONTAINER + "' AND "
                "in_window = 1 GROUP BY 1, 2 HAVING MAX(nr_throttled) > MIN(nr_throttled)."),
            "evidence": {
                "container": label,
                "throttled_periods": throttled,
                "total_periods": periods,
                "throttled_fraction": round(ratio, 4),
                "runs_affected": runs_throttled,
                "runs": runs,
                **({"declared_cpu": format_cores(reserved)} if reserved is not None else {}),
            },
        }]
    return []


def campaign_advice(query_rows) -> dict[str, Any]:
    """Advice for a campaign, given a ``query_rows(sql) -> list[dict]`` callable.

    Returns ``{"advice": [...]}`` -- a key rather than a bare list so a caller can merge it
    into a larger summary, and so a future non-resource advice source has somewhere to land.
    """
    declared = query_rows(DECLARED_SQL)
    try:
        system_mem = query_rows(SYSTEM_MEM_SQL)
    except Exception:  # noqa: BLE001 - no such table on a campaign predating the probe
        system_mem = []
    try:
        throttle = query_rows(THROTTLE_SQL)
    except Exception:  # noqa: BLE001 - no such table on a campaign predating the probe
        throttle = []
    return {"advice": (throttle_advice(throttle, declared)
                       + resource_advice(query_rows(USAGE_SQL), declared, system_mem)
                       + shm_advice(query_rows(SHM_SQL), declared))}
