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
    """``{nr_periods, nr_throttled, throttled_usec}``, or ``{}`` where cgroup v2 is not there.

    Returns an empty mapping rather than raising or zero-filling: a runtime that cannot answer
    and a container that was never throttled are different facts, and zeros would make the
    first indistinguishable from the second in every aggregate. cgroup v1 exposes these under a
    different path and is deliberately not handled -- it contributes no columns instead.
    """
    try:
        with open(CPU_STAT_PATH, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return {}
    out = {}
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        if key in CPU_STAT_FIELDS:
            try:
                out[key] = int(value)
            except ValueError:
                pass
    return out


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


#: A probe is a callable returning ``{metric: value}``. Adding one is adding it here; nothing
#: else in the chain needs to know, because the CSV -> ``data.db`` ingest types columns from
#: what it finds.
PROBES = (cpu_stat_probe, memory_probe)


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
