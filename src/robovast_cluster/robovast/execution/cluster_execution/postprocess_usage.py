# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What the postprocessing pod itself cost, in the sampler's own columns.

Every *trial* container is measured; the pod that postprocesses a campaign reserved cores
and memory with nothing recording what it did with them. So its reservation could only ever
be defended as "no campaign has been killed yet", and a campaign that raised
``results_processing.resources`` had no way to see whether it needed to.

**It is ``system_usage``, not ``resource_usage``, and the distinction is load-bearing.**
``resource_usage`` is per *process* by contract and every reader aggregates it that way, so
a container-level figure written there would be summed as though it were a process (see
:mod:`robovast.results_processing.system_usage`, which exists for exactly this reason). What
this records has no pid: it is the cgroup's own accounting for a whole step.

**The reading is the sampler's, not ours.** ``monitor_resources`` already reads every counter
this needs -- ``memory.peak`` and ``memory.max`` including cgroup v1's differently-named
equivalents, ``cpu.stat``'s throttle counters, the cgroup's billed CPU, and ``oom_kills`` from
``memory.events`` -- and ``system_usage`` is deliberately column-generic so that adding a
counter is a change to the sampler and to nothing else. Re-reading those files here would
have made the column list repeated in six places instead of one, with this copy free to
disagree about names and units. So this module decides only *where* the row goes and *how it
reads in the log*; :func:`~robovast.execution.data.monitor_resources.write_once` produces it.

**One row per step, read at the end rather than sampled.** The daemon's per-second rows are
sliced into runs; these steps are containers, not runs, and each is short -- so a poll
interval longer than the step reports a comfortable figure for one that may have come close
to its ceiling, and ``memory.peak`` is a high-water mark the kernel already keeps that no
sampler can beat.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Campaign-relative path of the record. Under ``_execution/`` because it describes how the
#: campaign was *processed*, not what its runs did -- a file under a run directory would be
#: swept into that run's metric tables and read as something the run itself consumed. Named
#: for the sampler's container-level half, which is what it holds.
USAGE_REL = "_execution/postprocess_system_usage.csv"


def record(campaign_dir: str, step: str) -> dict:
    """Append *step*'s container-level counters to the campaign's record.

    Returns ``{column: value}`` for what was written, empty when nothing could be.

    **Never raises.** This measures the postprocessing; it is not part of it. A campaign whose
    results were derived correctly must not fail because the record of what that cost could
    not be written.
    """
    try:
        from robovast.execution.data import monitor_resources  # noqa: PLC0415

        path = os.path.join(campaign_dir, *USAGE_REL.split("/"))
        probes = monitor_resources.start_probes()
        columns = [monitor_resources.ONCE_LABEL_COLUMN] + [
            column for _, cols in probes for column in cols]
        return dict(zip(columns, monitor_resources.write_once(path, step, probes)))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("could not record what the %s step used: %s", step, exc)
        return {}


def summary_line(row: dict) -> str:
    """The row as one line for the postprocessing log.

    In the log as well as the file because the question it answers -- did this step come near
    its limit? -- is asked while reading that log, and needing to download a campaign to
    answer it means nobody does.

    Reads the columns it knows and ignores the rest: the sampler decides what a container can
    report, so a counter added there must not need a change here, and one absent on a given
    kernel must not turn this into an error.
    """
    if not row:
        return "step usage not recorded"

    def _gib(value):
        try:
            return f"{int(value) / 1024 ** 3:.2f}GiB"
        except (TypeError, ValueError):
            return "?"

    parts = [f"{row.get('step', '?')} used {_gib(row.get('memory_peak'))} peak of "
             f"{_gib(row.get('memory_max'))}"]
    try:
        parts.append(f"{int(row['cpu_usage_usec']) / 1e6:.1f}s cpu")
    except (KeyError, TypeError, ValueError):
        pass
    try:
        periods = int(row["nr_periods"])
        if periods > 0:
            parts.append(f"throttled {int(row['nr_throttled']) / periods:.1%} "
                         "of enforcement periods")
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    # Only when it happened. A zero here is the normal case and saying so every time would
    # train the reader to skip the line that matters.
    try:
        if int(row["oom_kills"]) > 0:
            parts.append(f"OOM-KILLED {row['oom_kills']}x")
    except (KeyError, TypeError, ValueError):
        pass
    return ", ".join(parts)


def shell_record(campaign_dir: str, step: str) -> str:
    """Shell that records *step*, for the container this package cannot run in.

    The conversion container is the campaign's **own** image -- an arbitrary user image -- so
    nothing of ``robovast`` is importable there. What it does have is the ``/scripts`` mount,
    which carries ``monitor_resources.py`` for this, and a ``python3`` (it runs
    ``rosbags_process.py``). So the same reader produces the row, invoked as a script rather
    than imported.

    Ends in ``true`` and swallows its own failure: a conversion whose outputs are correct must
    not fail because the record of what it cost could not be written.
    """
    path = os.path.join(campaign_dir, *USAGE_REL.split("/"))
    return (f"python3 /scripts/monitor_resources.py --once {_shquote(path)} "
            f"{_shquote(step)} || true")


def _shquote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"
