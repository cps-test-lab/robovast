# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""The one way to recover a campaign's :class:`Status` from disk.

While a controller drives a campaign, its live :class:`Status` lives in the
in-memory :class:`~robovast.execution.control_server.ControllerState`. Once no
process is driving it (a past campaign, or one lost to a service restart), the
status has to be reconstructed from what is on disk. This module is the *single*
implementation of that reconstruction — it was previously duplicated (with
subtly different results) in ``service/client.py`` and the ``execution``
MCP plugin.

Precedence, loud and fixed:

1. ``_execution/outcome.json`` — the durable terminal record the controller
   writes on any terminal exit (finished / failed / stopped / crashed). This is
   the canonical status journal and wins when present — with the two exceptions
   spelled out below. A record that is *not* terminal is reported as ``crashed``:
   the finish tail journals one before the campaign ends, and reconstruction only
   happens when nothing drives the campaign any more, so a non-terminal record means
   the driver went away without ever recording an ending.
2. Otherwise, derive from the on-disk result artifacts (each run's ``test.xml``):
   ``finished`` only when the verdicts are *complete*, ``crashed`` when they are not.
   Deriving ``finished`` unconditionally reported a campaign whose service had just
   been restarted out from under it — jobs still running, half its runs missing — as
   finished, which is the answer that stops a reader from ever looking again. Nothing
   may claim an ending it has no evidence for.
3. A campaign directory that does not exist is ``unknown`` — genuinely
   unrecoverable, reported as such rather than guessed.

The one thing ``outcome.json`` does *not* get the last word on is the run tally.
It journals how the campaign **ended**; the store's ``run`` table records what its
runs **did**, so the tally comes from there whenever the table has rows. Without
that split a record written before the controller learned to count failing trials —
or by one that never saw the verdicts — reports ``failed: 0`` for a campaign whose
trials failed, and every reader (CLI, MCP, web UI) then paints it green. Neither
source is asked to guess: what has no verdict on disk is reported as resultless
rather than assumed to have passed.

This module depends only *downward* on ``robovast.common`` (the outcome/data
readers and the results store); nothing here reaches back up into ``service`` or
``mcp_server``.
"""

from pathlib import Path
from typing import Optional

from robovast.common.campaign_data import (get_vast_configuration_info,
                                           read_execution_outcome,
                                           write_execution_outcome)
from robovast.common.store import read_campaign_mode, read_run_counts
from robovast.execution.control_server import Phase, Status, is_terminal


def _runs_from_verdicts(counts: dict, total: int) -> dict:
    """A :class:`~robovast.client.status.RunProgress` payload from verdict tallies.

    *counts* carries the ``num_runs`` / ``num_passed`` / ``num_failed`` /
    ``num_errors`` / ``num_killed`` keys that both :func:`read_run_counts` (the store's
    ``run`` table) and :func:`get_vast_configuration_info` (the ``test.xml`` walk)
    return — the two ways this module learns what the runs did. A run that errored is a
    failure like any other, matching how ``CampaignSummary.num_failed`` is tallied; a run
    with no verdict at all delivered no result, which is the other failure axis and must
    not be folded into the passing ones.

    ``killed`` — a job an operator stopped by hand — is carried through rather than
    recomputed. It is a subset of ``no_result`` (a killed run delivered nothing, which is
    why it was recorded) and is deliberately **not** in ``failed``: reconstructing a
    campaign from disk must not turn a human intervention into a trial failure, which is
    exactly what dropping it did — the live status said ``killed: 1`` and the recovered
    one said ``0``, for the same campaign.
    """
    failed = counts.get("num_failed", 0) + counts.get("num_errors", 0)
    completed = counts.get("num_passed", 0) + failed
    # A recovered status reports the whole campaign, so `total` can only grow here: a
    # search campaign's durable record counts its last batch while the run table counts
    # every batch, and "80 completed of 16" is not a thing a reader can render.
    total = max(total, counts.get("num_runs", 0), completed)
    return {"completed": completed, "total": total, "failed": failed,
            "killed": counts.get("num_killed", 0),
            "no_result": max(0, total - completed)}


def reconstruct_status_from_disk(campaign_dir: str | Path,
                                 *, expected_total: Optional[int] = None) -> Status:
    """Recover a campaign's :class:`Status` from its on-disk artifacts.

    Args:
        campaign_dir: The ``campaign-<id>`` directory.
        expected_total: The number of runs the campaign was expected to produce,
            when a caller knows it (e.g. the MCP registry entry). Used only for the
            derived-from-artifacts case to report ``runs.total``; the durable
            ``outcome.json`` carries its own totals and ignores this.

    Returns:
        The durable ``outcome.json`` Status when present, with its run tally taken
        from the store's ``run`` table where that has rows; otherwise a derived
        ``finished`` Status tallying each run's verdict on disk; otherwise an
        ``unknown`` Status for a missing directory.
    """
    campaign_dir = Path(campaign_dir)
    campaign_id = campaign_dir.name
    if not campaign_dir.is_dir():
        return Status(phase=Phase.UNKNOWN, campaign_id=campaign_id)

    # ``postprocessed`` is a fact about the campaign, not about who last drove it:
    # the built ``data.db`` is the ground truth (postprocessing can chain *after*
    # ``outcome.json`` is written, so the durable record can say False while the
    # derived data is present). Recover it here so every disk-recovered Status
    # reports it consistently — the single recovery path stays authoritative.
    postprocessed = (campaign_dir / "_execution" / "data.db").is_file()

    # Who counted the runs: the store's ``run`` table, in one indexed read. ``None``
    # when there is no store to ask (absent, or a schema-v1 one with no such table),
    # and no rows when nothing was recorded — either way the tally comes from the
    # artifact walk below instead.
    counts = read_run_counts(campaign_dir)
    if counts is not None and counts["num_runs"] == 0:
        counts = None

    # The durable terminal record wins — prefer it over reconstructing a "finished"
    # from artifacts (it also carries the real phase: failed / stopped). Its run tally
    # is the exception: it may never have been given the verdicts, so the run table
    # supersedes it where there is one (see the module docstring).
    outcome = read_execution_outcome(campaign_dir)
    if outcome is not None:
        outcome.postprocessed = outcome.postprocessed or postprocessed
        if counts is not None:
            outcome.runs = _runs_from_verdicts(counts, outcome.runs.total)
        # A record that is not terminal was written mid-flight — the finish tail
        # journals one before the campaign ends — and reconstruction only happens when
        # nothing is driving the campaign any more. So the driver went away without
        # ever recording an ending: that is `crashed`, not "still finishing". Handing
        # back the non-terminal phase would leave every waiter blocked on a campaign
        # nobody is going to advance.
        if not is_terminal(outcome.phase):
            outcome.phase = Phase.CRASHED
        return outcome

    # No durable record. Nothing here may claim a run passed without a verdict saying
    # so: this used to report `completed == total`, painting a campaign green having
    # never looked at what its runs did. The artifact walk carries the verdicts too, so
    # it stands in for the store when there is none.
    if counts is None:
        try:
            counts = get_vast_configuration_info(campaign_dir)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            counts = {}
    runs = _runs_from_verdicts(counts, expected_total or counts.get("num_runs", 0))
    # `finished` is a claim, and without a durable record the only evidence for it is
    # the artifacts. A complete set of verdicts is that evidence; an incomplete one is
    # evidence of the opposite. Deriving `finished` either way reported a campaign whose
    # driver had just been restarted out from under it — jobs still running, half its
    # runs missing — as a finished campaign, which is the answer that stops a reader
    # (and a waiter) from ever looking again.
    complete = runs["completed"] > 0 and runs["no_result"] == 0
    return Status(phase=Phase.FINISHED if complete else Phase.CRASHED,
                  campaign_id=campaign_id,
                  mode=read_campaign_mode(campaign_dir),
                  postprocessed=postprocessed,
                  runs=runs)


def record_step_outcome(campaign_dir: str | Path, *,
                        postprocessing: Optional[tuple] = None,
                        share: Optional[tuple] = None) -> Status:
    """Persist a re-triggered post-run step's result into ``_execution/outcome.json``.

    A re-trigger (``run_postprocessing`` / ``run_share``) runs on a campaign that is
    no longer driven in-process — there is no live ``ControllerState`` to update — so
    the durable outcome is edited straight on disk. The current Status is reconstructed
    (preferring an existing ``outcome.json``, so a prior share/postproc marker is
    preserved), the given step's field is refreshed, and it is written back.

    Each of *postprocessing* / *share*, when given, is an ``(ok, message)`` pair; only
    the provided step is touched. ``phase`` is normalised to ``finished`` — a re-trigger
    runs on an already-finished campaign and never changes that.

    Returns the written :class:`Status`.
    """
    campaign_dir = Path(campaign_dir)
    status = reconstruct_status_from_disk(campaign_dir)
    status.phase = Phase.FINISHED
    if postprocessing is not None:
        ok, message = postprocessing
        # ``postprocessed`` follows the on-disk data.db (reconstruct already set it);
        # here we only clear/set the failure marker.
        status.postprocessing_error = None if ok else message
    if share is not None:
        ok, message = share
        status.share_error = None if ok else message
    write_execution_outcome(campaign_dir, status)
    return status
