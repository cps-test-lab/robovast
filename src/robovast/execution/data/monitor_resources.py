#!/usr/bin/env python3
"""Resource monitoring daemon - records per-process CPU/memory at 1s intervals to a CSV file."""
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

_shutdown = False  # pylint: disable=invalid-name


def _handle_signal(signum, frame):  # pylint: disable=unused-argument
    global _shutdown  # pylint: disable=global-statement
    _shutdown = True


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


def main():
    output_path = sys.argv[1] if len(sys.argv) > 1 else "/out/resource_usage.csv"

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Prime cpu_percent measurements (first call per process always returns 0.0)
    for proc in psutil.process_iter(["cpu_percent"]):
        pass

    with open(output_path, "w", newline="", buffering=1) as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "pid", "name", "cpu_percent", "memory_rss_bytes",
                         "shm_used_bytes", "shm_total_bytes"])

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

            # ensure rows survive sudden kill
            f.flush()
            os.fsync(f.fileno())

            # Sleep in short increments so SIGTERM is handled promptly
            deadline = ts + 1.0
            while not _shutdown and time.time() < deadline:
                time.sleep(0.1)


if __name__ == "__main__":
    main()
