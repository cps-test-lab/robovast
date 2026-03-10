#!/usr/bin/env python3
"""Resource monitoring daemon - records per-process CPU/memory at 1s intervals to a CSV file."""
import csv
import signal
import sys
import time
import os
import psutil

_shutdown = False


def _handle_signal(signum, frame):  # pylint: disable=unused-argument
    global _shutdown  # pylint: disable=global-statement
    _shutdown = True


def main():
    output_path = sys.argv[1] if len(sys.argv) > 1 else "/out/resource_usage.csv"

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Prime cpu_percent measurements (first call per process always returns 0.0)
    for proc in psutil.process_iter(["cpu_percent"]):
        pass

    with open(output_path, "w", newline="", buffering=1) as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "pid", "name", "cpu_percent", "memory_rss_bytes"])

        while not _shutdown:
            ts = time.time()
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
