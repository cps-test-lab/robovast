#!/usr/bin/env python3
"""Resource monitoring daemon - records per-process CPU/memory at 1s intervals to a CSV file.

It writes **two** files per tick, because there are two kinds of metric here and mixing them
has a cost. ``resource_usage_<container>.csv`` is per *process*, by contract: every reader
aggregates it that way, so a container-level figure written as a synthetic process row would
be summed as though it were one. ``system_usage_<container>.csv`` is the sibling for figures
that belong to the container as a whole -- one row per tick, no ``pid``.
"""
import csv
import os
import signal
import sys
import time

import psutil

#: The shared-memory pool, sampled once per tick alongside the process rows. It is ONE tmpfs
#: for the whole run -- the pod's `dshm` volume mounted into every container on the cluster
#: lane, the main container's `/dev/shm` shared through its IPC namespace locally -- so the
#: figure is a property of the tick, not of a process, and is repeated across the tick's rows.
#: Nothing else reports it: a container that overruns shared memory dies of SIGBUS (exit 135)
#: rather than a clean OOM, so without this the death arrives with no number behind it.
SHM_PATH = "/dev/shm"

#: cgroup v2's CPU accounting for the whole container. ``nr_throttled`` and ``throttled_usec``
#: are the only place anything states that the kernel *stopped* this container because it hit
#: its CPU quota. Nothing else in a campaign records that: a throttled run does not fail, it
#: just gets slower, and its results quietly become partly a measurement of the allocation
#: rather than of the system under test. ``nr_periods`` comes along because the ratio
#: ``nr_throttled / nr_periods`` is the figure worth reading -- a raw count means nothing
#: without knowing how many enforcement windows it was drawn from.
CPU_STAT_PATH = "/sys/fs/cgroup/cpu.stat"
CPU_STAT_FIELDS = ("nr_periods", "nr_throttled", "throttled_usec")

#: The same counters under cgroup **v1**, which is not a legacy concern: one node of the
#: cluster this was written against runs an older distribution and is the largest machine in
#: it, so skipping v1 left ~48% of a campaign's runs unmeasured -- and, because the scheduler
#: packs by core count, they were the runs on the node that attracted the most work and
#: produced every observed control-loop miss. A blind spot that tracks node size is worse than
#: a uniform one: the aggregate looks fine and is drawn from the machines under least
#: pressure.
CPU_STAT_PATH_V1 = "/sys/fs/cgroup/cpu/cpu.stat"

#: v1 spells the third counter ``throttled_time`` and reports it in **nanoseconds**, where v2
#: uses ``throttled_usec`` in microseconds. Converted on read rather than stored as it comes,
#: because one column that silently means nanoseconds on some nodes and microseconds on others
#: is worse than the column being absent -- absence is visible, a 1000x unit error is not.
CPU_STAT_V1_USEC_FIELD = "throttled_time"
_NSEC_PER_USEC = 1000

#: Filename prefixes. The sibling is derived from the process file's own name (see
#: :func:`system_usage_path`) so that the launch contract -- which entrypoint passes which
#: path, pinned by ``tests/execution/test_resource_monitor_lanes.py`` -- needs no change, and
#: neither entrypoint script has to learn about a second file.
PROCESS_PREFIX = "resource_usage_"
SYSTEM_PREFIX = "system_usage_"

_shutdown = False  # pylint: disable=invalid-name


def _handle_signal(signum, frame):  # pylint: disable=unused-argument
    global _shutdown  # pylint: disable=global-statement
    _shutdown = True


def cpu_stat_probe():
    """``{nr_periods, nr_throttled, throttled_usec}``, or ``{}`` where neither cgroup has them.

    Returns an empty mapping rather than raising or zero-filling: a runtime that cannot answer
    and a container that was never throttled are different facts, and zeros would make the
    first indistinguishable from the second in every aggregate.

    **Both cgroup versions**, v2 first. v1 was skipped when this was written, on the reasoning
    that it is old; the cost turned out to be node-shaped rather than small (see
    :data:`CPU_STAT_PATH_V1`). The two are read into the *same* column names and the same
    units, so a campaign spanning both kinds of node stays comparable -- which is the whole
    point, since the interesting question is per-node.
    """
    for path, usec_field in ((CPU_STAT_PATH, "throttled_usec"),
                             (CPU_STAT_PATH_V1, CPU_STAT_V1_USEC_FIELD)):
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            continue
        out = {}
        for line in raw.splitlines():
            key, _, value = line.partition(" ")
            if key not in ("nr_periods", "nr_throttled", usec_field):
                continue
            try:
                parsed = int(value)
            except ValueError:
                continue
            if key == usec_field:
                out["throttled_usec"] = (parsed // _NSEC_PER_USEC
                                         if usec_field == CPU_STAT_V1_USEC_FIELD else parsed)
            else:
                out[key] = parsed
        # A file that exists but yields nothing recognisable is not an answer; fall through to
        # the other layout rather than reporting a half-filled row for it.
        if out:
            return out
    return {}


#: cgroup v2's memory accounting for the whole container -- what the kernel actually enforces
#: the limit against. The per-process rows cannot answer this: RSS counts a shared page once
#: per process, so summing them over a stack of forty ROS nodes sharing libraries and a Fast
#: DDS shared-memory segment over-reports badly. Measured on one campaign: summed RSS peaked at
#: 5147 MiB in a container running comfortably under a 2944 MiB limit, while its largest single
#: process held 1014 MiB. ``memory.peak`` is the high-water mark and needs kernel 5.19+.
MEMORY_PATH = "/sys/fs/cgroup"
MEMORY_FILES = ("memory.current", "memory.peak", "memory.max")


def memory_probe():
    """Container memory as the kernel accounts it, or ``{}`` where it cannot be read.

    ``memory.max`` reads ``max`` when no limit is set; that is recorded as an absence rather
    than as a number, because "unlimited" and "some very large limit" are different facts and
    only one of them can be compared against usage.
    """
    out = {}
    for name in MEMORY_FILES:
        try:
            with open(os.path.join(MEMORY_PATH, name), encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        try:
            out[name.replace(".", "_")] = int(raw)
        except ValueError:
            pass  # "max" -- no limit in force
    return out


#: cgroup v2's own CPU accounting for the container, in microseconds. The per-process rows
#: cannot replace it: ``cpu_percent`` is psutil's sampled estimate, it misses a process that
#: lived and died between two ticks entirely, and the sum over forty ROS nodes accumulates
#: forty roundings. This is the number the kernel bills the cgroup, and a delta over a run
#: window divided by its wall span is the container's mean cores exactly rather than
#: approximately -- which matters because the sizing rule in ``advice`` is a percentile of
#: that figure, and a percentile of an estimate is an estimate of a percentile.
CPU_USAGE_FIELD = "usage_usec"

#: v1 keeps the same quantity in a different controller and in NANOseconds. Converted on read
#: for the reason :data:`CPU_STAT_V1_USEC_FIELD` gives: one column that means microseconds on
#: some nodes and nanoseconds on others is worse than no column, because absence is visible.
CPUACCT_USAGE_PATH = "/sys/fs/cgroup/cpuacct/cpuacct.usage"


def cpu_usage_probe():
    """``{cpu_usage_usec}`` -- CPU time billed to this cgroup, or ``{}``.

    Deliberately a separate probe from :func:`cpu_stat_probe` even though v2 keeps both in one
    file. The two answer different questions and are available independently: a cgroup with no
    CPU limit reports usage and no throttle counters at all, and reading them together would
    make the second's absence suppress the first.
    """
    try:
        with open(CPU_STAT_PATH, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(" ")
                if key == CPU_USAGE_FIELD:
                    return {"cpu_usage_usec": int(value)}
    except (OSError, ValueError):
        pass
    try:
        with open(CPUACCT_USAGE_PATH, encoding="utf-8") as handle:
            return {"cpu_usage_usec": int(handle.read().strip()) // _NSEC_PER_USEC}
    except (OSError, ValueError):
        return {}
    return {}


#: Pressure Stall Information for this container's own cgroup: how long its tasks were
#: RUNNABLE BUT NOT RUNNING.
#:
#: This is the counter ``cpu.stat`` cannot produce. Throttling records a container hitting its
#: OWN ceiling and by construction says nothing about one that was crowded out by a neighbour
#: -- the two even point opposite ways, since a container that cannot get CPU never reaches
#: its quota and so throttles LESS while running worse. On a lane where a container may
#: reserve less than its limit, "was this run slow because of what it asked for, or because of
#: what else was on the node?" is the question a reader actually has, and until this probe
#: only the first half of it was measured.
#:
#: ``some`` is time at least one task was stalled, ``full`` time every task was. For a
#: container running forty ROS nodes against three cores ``some`` is high in normal operation
#: -- there is nearly always one runnable thread waiting -- so it is recorded for completeness
#: while ``full`` is the figure that carries the finding: all work stopped, wanting CPU.
#:
#: **Throttling raises these too**, so a stall figure is only interpretable beside the throttle
#: ratio: stall WITH throttling is the container's own ceiling, stall WITHOUT it is contention.
#: Neither column answers alone, which is why ``run_validity_view`` combines them rather than
#: reporting a "starved" flag from this file by itself.
#:
#: Memory and IO use the identical format and cost one read each. They are here because they
#: are the other two ways a run is slow with nothing in the CPU counters to show for it: a
#: container in reclaim, and one waiting on a cold image layer or an asset load.
#:
#: ``total`` is the field taken -- a monotonic microsecond counter, which takes the same
#: in-window delta as ``throttled_usec`` and so needs no new machinery downstream. The
#: ``avgN`` fields on the same line are decaying averages over the last N seconds and cannot
#: be aggregated over a run window at all.
#:
#: cgroup v1 has no PSI, v2 needs ``CONFIG_PSI=y`` (and ``psi=1`` where a distribution
#: defaults it off), and cgroup-level ``full`` for CPU needs 5.13+. Every one of those is
#: reported as an absent column rather than a zero.
PRESSURE_FILES = (("cpu", "/sys/fs/cgroup/cpu.pressure"),
                  ("memory", "/sys/fs/cgroup/memory.pressure"),
                  ("io", "/sys/fs/cgroup/io.pressure"))


def _pressure_totals(path, prefix):
    """``{<prefix>_stall_some_usec, <prefix>_stall_full_usec}`` from one PSI file."""
    out = {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return out
    for line in raw.splitlines():
        fields = line.split()
        if not fields or fields[0] not in ("some", "full"):
            continue
        for field in fields[1:]:
            key, _, value = field.partition("=")
            if key != "total":
                continue
            try:
                out[f"{prefix}_stall_{fields[0]}_usec"] = int(value)
            except ValueError:
                pass
    return out


def pressure_probe():
    """The cgroup's CPU, memory and IO stall totals, or ``{}`` where PSI is unavailable.

    Partial answers are kept: a kernel old enough to report ``some`` and not ``full`` still
    contributes the column it has, and ``start_probes`` fixes the header from whatever came
    back once, so the CSV stays rectangular either way.
    """
    out = {}
    for prefix, path in PRESSURE_FILES:
        out.update(_pressure_totals(path, prefix))
    return out


#: The NODE's own CPU pressure, read from the host procfs the container shares. A *node* fact
#: repeated on every one of that container's rows, which is the one thing to know before
#: reading it: it is not attributable to this container and must never be summed across the
#: containers of a pod.
#:
#: It is here because it is the only signal that separates "this pod was unlucky" from "the
#: machine was oversubscribed": the cgroup's own stall says work was waiting, and this says
#: whether everything else on the box was waiting too. That comparison is exactly what decides
#: whether a request-below-limit split cost a campaign anything, and no aggregate over
#: per-container counters can stand in for it.
#:
#: Absent when procfs is masked or the kernel lacks PSI, which the usual contract covers.
NODE_PRESSURE_PATH = "/proc/pressure/cpu"


def node_pressure_probe():
    """``{node_cpu_stall_some_usec}``, or ``{}``.

    Only ``some``: at the system level the kernel documents ``full`` for CPU as undefined,
    and recording a field whose meaning the kernel disclaims would invite exactly the
    comparison it cannot support.
    """
    totals = _pressure_totals(NODE_PRESSURE_PATH, "node_cpu")
    some = totals.get("node_cpu_stall_some_usec")
    return {} if some is None else {"node_cpu_stall_some_usec": some}


#: What the container's memory is MADE OF, which decides how much of it has to be reserved.
#:
#: ``memory.current`` and ``memory.peak`` include page cache, and a container running under a
#: loose limit accumulates cache it never needed -- meshes, textures, the bag being written --
#: simply because the kernel had no reason to reclaim it. Sizing a limit from that figure
#: over-reserves, and does so worst for the simulator, which is the container most worth
#: shrinking. Under a tighter limit the same run reclaims and proceeds.
#:
#: ``anon`` + ``shmem`` + ``slab`` is the part that cannot be reclaimed under pressure and so
#: is the honest basis for a reservation; ``file`` is recorded beside them to make the
#: difference visible rather than assumed. ``shmem`` belongs in the first group: the DDS
#: segment in ``/dev/shm`` is charged to the cgroup and is not reclaimable while it is mapped.
#:
#: These are GAUGES, not counters -- read the max over the window, never a delta.
#:
#: v2 only, sharing :func:`memory_probe`'s coverage rather than inventing a second one: v1
#: spells every field differently (``rss``, ``cache``, ``mapped_file``), and one column that
#: means a slightly different quantity on some nodes is the failure mode the unit conversions
#: elsewhere in this file exist to avoid.
MEMORY_STAT_FILE = "memory.stat"
MEMORY_STAT_FIELDS = ("anon", "file", "shmem", "slab")


def memory_stat_probe():
    """``{memory_anon, memory_file, memory_shmem, memory_slab}``, or ``{}``."""
    out = {}
    try:
        with open(os.path.join(MEMORY_PATH, MEMORY_STAT_FILE), encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return out
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        if key not in MEMORY_STAT_FIELDS:
            continue
        try:
            out[f"memory_{key}"] = int(value)
        except ValueError:
            pass
    return out


#: cgroup v2's record of the kernel REFUSING an allocation. ``max`` counts the times usage
#: would have exceeded the limit and reclaim was forced instead; ``oom_kill`` counts the
#: processes killed for it.
#:
#: The sizing rule treats memory and CPU differently -- memory is sized on the peak because
#: exceeding it is a kill rather than a slowdown -- and this is the counter that says the rule
#: was violated. ``memory.peak`` shows a container that came close; only this shows one the
#: kernel acted against, and a run whose ROS node was OOM-killed otherwise reports a
#: mid-trial death with no reason attached to it.
MEMORY_EVENTS_PATH = "/sys/fs/cgroup/memory.events"
MEMORY_EVENT_FIELDS = ("max", "oom", "oom_kill")


def memory_events_probe():
    """``{memory_events_max, memory_events_oom, memory_events_oom_kill}``, or ``{}``."""
    out = {}
    try:
        with open(MEMORY_EVENTS_PATH, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return out
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        if key not in MEMORY_EVENT_FIELDS:
            continue
        try:
            out[f"memory_events_{key}"] = int(value)
        except ValueError:
            pass
    return out


#: A probe is a callable returning ``{metric: value}``. Adding one is adding it here; nothing
#: else in the chain needs to know, because the CSV -> ``data.db`` ingest types columns from
#: what it finds.
PROBES = (cpu_stat_probe, cpu_usage_probe, memory_probe, memory_stat_probe,
          memory_events_probe, pressure_probe, node_pressure_probe)


def start_probes(probes=PROBES):
    """The probes that actually answered, with the columns each produces.

    **Availability is decided once, at startup, not per tick.** That is what keeps the CSV
    header fixed for the file's lifetime, which the generic CSV ingest needs in order to type a
    column; a probe that came and went would produce a ragged file. It also means an
    unavailable probe costs one read at startup and nothing thereafter.
    """
    live = []
    for probe in probes:
        try:
            sample = probe()
        except Exception:  # noqa: BLE001 - a broken probe must not stop the sampler
            sample = None
        if sample:
            live.append((probe, tuple(sorted(sample))))
    return live


def system_usage_path(process_path):
    """The sibling file's path, beside the per-process one it is derived from.

    ``resource_usage_sut.csv`` -> ``system_usage_sut.csv``, so both halves carry the same
    container suffix and ``run_slices.container_of`` inverts them the same way.
    """
    directory, base = os.path.split(process_path)
    if base.startswith(PROCESS_PREFIX):
        base = SYSTEM_PREFIX + base[len(PROCESS_PREFIX):]
    else:
        base = SYSTEM_PREFIX + "main.csv"
    return os.path.join(directory, base)


def _shm_bytes():
    """``(used, total)`` of :data:`SHM_PATH`, or ``(None, None)`` if it cannot be read.

    ``total`` is the tmpfs's own size, which is the LIMIT actually in force -- what
    ``execution.shm_size`` asked for, or whatever default the lane handed out when it asked
    for nothing. Recording it is what makes a declaration checkable rather than assumed.

    Absence is written as an absence, never as ``0``: a runtime without ``/dev/shm`` and a run
    that used none of it are different answers, and a zero here would make the first look like
    the second in every aggregate.

    ``used`` counts against FREE blocks, not available ones, so it can read a little above
    ``df``'s Used column where the filesystem reserves some. That is the conservative side of
    the difference, and it is the figure the size limit is actually spent against.
    """
    try:
        st = os.statvfs(SHM_PATH)
    except OSError:
        return None, None
    return (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_blocks * st.f_frsize


def _system_row(probes):
    """One tick's values, in the same column order the header was written from.

    A probe that answered at startup and fails later writes blanks rather than taking the
    sampler down: the run's own measurement is worth more than this diagnostic, and a gap in a
    counter column is readable as a gap.
    """
    row = []
    for probe, columns in probes:
        try:
            sample = probe()
        except Exception:  # noqa: BLE001 - see docstring
            sample = {}
        row.extend(sample.get(column, "") for column in columns)
    return row


def main():
    output_path = sys.argv[1] if len(sys.argv) > 1 else "/out/resource_usage.csv"

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Prime cpu_percent measurements (first call per process always returns 0.0)
    for proc in psutil.process_iter(["cpu_percent"]):
        pass

    # Decided once. The header must be fixed for the file's lifetime, and a probe that came and
    # went would produce a ragged CSV the generic ingest could not type.
    probes = start_probes()
    system_columns = [column for _, columns in probes for column in columns]

    with open(output_path, "w", newline="", buffering=1) as f, \
            open(system_usage_path(output_path), "w", newline="", buffering=1) as sf:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "pid", "name", "cpu_percent", "memory_rss_bytes",
                         "shm_used_bytes", "shm_total_bytes"])
        # Written even when no probe answered, so the file's presence means "the sampler ran"
        # and its emptiness means "this runtime reports nothing" -- which a missing file cannot
        # distinguish from a sampler that died.
        system_writer = csv.writer(sf)
        system_writer.writerow(["timestamp"] + system_columns)

        while not _shutdown:
            ts = time.time()
            # Once per tick, not once per process: one syscall a second, and every row of the
            # tick would otherwise report the same pool at a slightly different instant.
            shm_used, shm_total = _shm_bytes()
            for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                try:
                    info = proc.info
                    mem = info["memory_info"]
                    writer.writerow([
                        ts,
                        info["pid"],
                        info["name"],
                        info["cpu_percent"],
                        mem.rss if mem else 0,
                        "" if shm_used is None else shm_used,
                        "" if shm_total is None else shm_total,
                    ])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            system_writer.writerow([ts] + _system_row(probes))

            # ensure rows survive sudden kill
            f.flush()
            os.fsync(f.fileno())
            sf.flush()
            os.fsync(sf.fileno())

            # Sleep in short increments so SIGTERM is handled promptly
            deadline = ts + 1.0
            while not _shutdown and time.time() < deadline:
                time.sleep(0.1)


if __name__ == "__main__":
    main()
