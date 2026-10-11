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

"""A campaign's status and the campaign listing, as the fields a caller reads.

One rendering for every surface that answers "how is this campaign doing?" and "what has
been run?": the MCP tools ``get_campaign_status`` and ``list_campaigns`` return these dicts,
and ``vast campaign status --json`` / ``vast campaign list --json`` print them. Here, in the
client distribution, because the CLI must answer without the core installed.
"""

import time

from robovast.client.status import (HEALTH_NEXT_STEP, budget_positions,
                                    error_findings, is_running, stall_report,
                                    stopping_soon_report)


def status_report(client, campaign_id: str) -> dict:
    """The status of *campaign_id* as *client* reports it, with what to do next.

    Raises whatever the client raises; the caller decides how a failure is reported.
    """
    st = client.get_status(campaign_id)
    result = status_to_dict(campaign_id, "service", st)
    result["stage"] = st.stage or ""  # a live marker string, not a log tail
    if (st.mode or "").lower() == "search":
        attach_objective_history(result, client, campaign_id)
    next_step = campaign_next_step(result)
    if next_step:
        result["next_step"] = next_step
    return result


def campaign_listing(client, request, running_only: bool = False) -> dict:
    """``{campaigns, total, offset}`` for a ``ListCampaignsRequest``, each campaign as
    :func:`summary_to_dict` renders it.

    *running_only* keeps the live campaigns, however old, and ``total`` counts them.
    """
    if running_only:
        matched = [c for c in walk_all(client, request.sort, request.order)
                   if is_running(c.phase)]
        total = len(matched)
        window = matched[request.offset:request.offset + request.limit]
    else:
        page = client.list_campaigns(request)
        total = page.total
        window = page.campaigns
    return {"campaigns": [summary_to_dict(c) for c in window], "total": total,
            "offset": request.offset}


def binding_budget(st):
    """The budget row closest to exhausting and its share, or ``None`` when none is usable.

    The campaign stops at whichever criterion fires first, so the closest one is the only
    one describing a moment this campaign will actually reach; any other describes a moment
    it never will.

    Read through :func:`budget_positions`, so a ``time`` budget reports where the search is
    now rather than where it was when the last round closed.

    The single implementation of that rule in Python: :func:`progress_from_status` takes
    the share from here and the status dict takes the row, so the reported progress and the
    criterion it is named against cannot disagree. The web UI's ``ringBudget`` (lib/eta.ts)
    applies the same rule to draw the ring, and ``campaignEtaSeconds`` expresses it in time
    units where "fires first" is the *smaller* duration -- a change to any of the three must
    be made looking at the other two.
    """
    best, best_share = None, -1.0
    for b in budget_positions(st):
        if b.current is None or not b.limit:
            continue
        share = max(0.0, min(1.0, b.current / b.limit))
        if share > best_share:
            best, best_share = b, share
    return None if best is None else (best, best_share)


def progress_from_status(st) -> float | None:
    """Overall progress in ``[0, 1]``, or ``None`` when it cannot be known honestly.

    - **batch** mode: ``completed / total`` — the total is known up front.
    - **search** mode: the loop ends when a stopping criterion fires, so progress is
      the closest criterion, ``max(current / limit)`` over the ``budget``. A search's
      per-batch run ratio is deliberately **not** used — it would read as overall
      completion when it is only progress through one batch of an open-ended search.

    The search case is :func:`binding_budget`'s share -- see there for the rule and for the
    two other readers that must agree with it.

    Returns ``None`` (never a misleading number) when a search has no usable budget
    value yet.
    """
    if st.budget:
        binding = binding_budget(st)
        return binding[1] if binding else None
    mode = (st.mode or "").lower()
    if mode in ("", "batch") and st.runs and st.runs.total:
        return max(0.0, min(1.0, st.runs.completed / st.runs.total))
    return None


#: How many batches of the objective trajectory ride along on a status read. Bounded on purpose:
#: this is an agent's context, which is the scarce resource here, and the useful signal for "is it
#: still improving?" is the recent shape plus the level already reached — which the window's first
#: `best_so_far` still carries. The whole history is queryable from `campaign.db` once the campaign
#: ends; this is the live read.
OBJECTIVE_HISTORY_WINDOW = 20


def attach_objective_history(result: dict, client, campaign_id: str) -> None:
    """Add a search's objective trajectory to a status dict, in place.

    On the status report rather than behind a call of its own: a second call has to be
    discovered, and an agent that must remember a follow-up does not make it. It stays off the
    HTTP status, which the web UI polls, because this report is an occasional read.

    ``batches_since_improvement`` is a FACT, not a verdict. Whether a flat stretch means "converged"
    is only RoboVAST's to say when the campaign declared a ``no_improvement`` criterion — and then
    ``budget`` already carries that criterion's progress and the campaign will stop itself. Same
    rule as ``stalled: None`` when no timeout is declared: no verdict is possible, which is not the
    same as "healthy".

    A service that cannot answer does not fail the read, whose job is the phase; the failure is
    reported as ``objective_history_error`` so it does not read as a search with no history.
    """
    try:
        history = client.get_search_history(campaign_id)
    except Exception as e:  # noqa: BLE001 - a status read must not fail over its garnish
        result["objective_history_error"] = str(e)
        return
    if history.unavailable:
        # Named rather than silent: "several objectives, so there is no scalar to trend" is a
        # different fact from "this search has found nothing", and an absent field reads as the
        # second one.
        if history.unavailable == "multi_objective":
            result["objective_history_unavailable"] = history.unavailable
        return
    batches = [b for b in history.batches if b.n_scored]
    if not batches:
        return
    best = batches[-1].best_so_far
    since = 0
    # A batch still running has not yet been a round without improvement.
    for b in reversed([b for b in batches if b.complete]):
        if b.best_so_far != best:
            break
        since += 1
    result["objective_name"] = history.objective_name
    result["objective_direction"] = history.direction
    # Rounds completed since the best last MOVED, so the round that set it does not count itself.
    result["batches_since_improvement"] = max(0, since - 1)
    window = batches[-OBJECTIVE_HISTORY_WINDOW:]
    omitted = len(batches) - len(window)
    if omitted:
        result["objective_history_omitted"] = omitted
    result["objective_history"] = [b.model_dump() for b in window]


def status_to_dict(campaign_id: str, backend, st) -> dict:
    """Render a controller :class:`Status` into the status report's fields.

    Faithful to both batch and search campaigns: run counts are **batch-scoped**
    (``batch_runs_*``) and ``progress`` is computed mode-aware (see
    :func:`progress_from_status`), while the search-only fields (best objective,
    budget, batches done, stop reason) are surfaced when present.
    """
    result: dict = {
        "campaign_id": campaign_id,
        "backend": backend,
        "status": st.phase,
        "mode": st.mode,
        "batch_runs_done": st.runs.completed if st.runs else 0,
        "batch_runs_total": st.runs.total if st.runs else 0,
        # Two distinct outcomes, because a run can deliver nothing *or* deliver a
        # failing trial, and reporting only the former made a sweep with a failed
        # trial look clean. See RunProgress.
        "batch_runs_no_result": st.runs.no_result if st.runs else 0,
        "batch_runs_failed": st.runs.failed if st.runs else 0,
        # Whether the two counts above are final for this batch, because 0 alone cannot
        # say. They are written once, when the batch's verdicts are tallied, so a poll
        # partway through a batch that had already lost runs reads 0 -- which is also what
        # a batch that lost nothing reads. Reported beside them rather than left to the
        # docstring: a caller that has not read the docstring is exactly the caller that
        # misreads the number.
        "batch_outcomes_counted": bool(st.runs.outcomes_counted) if st.runs else False,
        "progress": progress_from_status(st),
    }
    # How long the campaign has held this phase. A phase alone cannot separate slow
    # from wedged: an image build and a build that will never finish both read
    # "building", and a pre-run step that hangs is otherwise invisible until someone
    # notices the run count has not moved.
    if getattr(st, "phase_since", None):
        result["phase_age_s"] = round(max(0.0, time.time() - st.phase_since), 1)
    # Progress age and the stall verdict, derived once in the status contract so the
    # CLI monitor and this tool cannot disagree about whether a run is wedged.
    result.update(stall_report(st))
    # The early-stop verdict, from the same contract and for the same reason: an agent weighing
    # `progress: 0.67` against a flat objective has to know whether the search will actually
    # spend the rest of its budget. `attach_objective_history` reports
    # `batches_since_improvement` as a FACT and declines to judge it -- correctly, since the
    # judgement is only RoboVAST's to make when the campaign declared a criterion. When it did,
    # this is that judgement.
    result.update(stopping_soon_report(st))
    # Only when a running job's simulator reported one, but then always: an error-level finding
    # is what stops `vast campaign wait` (HEALTH_FINDING), so a reader of this report has to be
    # shown the same thing the waiter was. Warnings are deliberately absent -- they never end a wait, and a field that
    # is populated on healthy campaigns is one readers learn to skip. ``get_job_state`` has them.
    findings = error_findings(st)
    if findings:
        result["health_findings"] = [f.model_dump() for f in findings]
        result["health_next_step"] = HEALTH_NEXT_STEP
    # Beside the findings and only with them: a check that reached no verdict matters precisely
    # when something else did fire, because that is when a reader starts treating the rest of the
    # run as fine. On its own it is noise on every healthy campaign.
    if findings and st.health_skipped:
        result["health_checks_not_run"] = list(st.health_skipped)
    # Only when it happened, but then always, and NOT gated on a finding: a campaign running on
    # fewer machines than the cluster has is slower than its plan and says so nowhere else while
    # it runs. It is a fact about the campaign, not a diagnostic about a job, so it is reported
    # on its own rather than beside the health block.
    if st.nodes_skipped:
        result["nodes_skipped"] = dict(st.nodes_skipped)
    # Only when it happened, but then always: a killed run is inside ``no_result``, so
    # without this the count reads as a run that vanished on its own rather than one
    # somebody deliberately ended — and the reader goes looking for a fault there is none.
    if st.runs and st.runs.killed:
        result["batch_runs_killed"] = st.runs.killed
    # Same rule, and the sharper case: an invalidated run may have written a PASSING
    # verdict against a container that had lost its state. Silence here would leave a
    # reader counting it among the results.
    if st.runs and st.runs.invalid:
        result["batch_runs_invalid"] = st.runs.invalid
    if st.batches_done:
        result["batches_done"] = st.batches_done
    if st.best_objective is not None:
        result["best_objective"] = st.best_objective
    if st.budget:
        # Through budget_positions, so a `time` row reports where the search is now rather than
        # where it was when the last round closed.
        result["budget"] = [b.model_dump() for b in budget_positions(st)]
        # Which criterion `progress` is a share OF. A bare 0.67 does not say whether that is
        # runs, rounds, evaluations or seconds, and an agent should not have to re-derive the
        # max to find out -- nor guess, since the answer changes which criterion it should
        # weigh a stall or a flat objective against.
        binding = binding_budget(st)
        if binding is not None:
            result["progress_of"] = binding[0].kind or binding[0].label
    if st.stop:
        result["stop"] = st.stop
    if st.error:
        result["error"] = st.error
    # Postprocessing is a separate fact from ``phase`` on purpose (see Status): a
    # campaign whose runs all passed but whose postprocessing failed stays
    # ``finished``, because the runs are the deliverable. That only works if the fact
    # is *reported* — folded into ``stage`` it reads like a progress note, and a
    # campaign with no metrics at all looks as green as a complete one.
    result["postprocessed"] = st.postprocessed
    if st.postprocessing_error:
        result["postprocessing_error"] = st.postprocessing_error
    if st.share_error:
        result["share_error"] = st.share_error
    return result


def campaign_next_step(result: dict) -> str:
    """What to do about the campaign state just reported, or "" when nothing is obvious.

    The same reason an image build's status carries one: a caller reads this to
    decide, and leaving that decision to be *remembered* is the defect
    :data:`~robovast.client.status.STALL_NEXT_STEP` is written against. Empty when the
    campaign is simply progressing, per AGENTS.md: a hint on every reply is a field callers
    learn to skip.

    Ordered cheapest-first where a stall is reported, because the untainted options come
    before anything that perturbs the run.
    """
    findings = result.get("health_findings") or []
    if findings:
        # Before the stall verdict deliberately: a finding names a fault class ("sim time is not
        # advancing") where a stall says only "nothing finished in time", and it is true within a
        # minute of the fault rather than one declared budget later.
        # Not the stall's step: that one sends a reader to ask what the job is doing, which
        # the finding has already answered.
        first = findings[0]
        return (f"{first.get('job_name', '')}: {first.get('check', '')} — "
                f"{first.get('detail', '')}. Next: {HEALTH_NEXT_STEP}")
    if result.get("stalled") is True:
        return result.get("stall_reason", "")
    if result.get("status") == "finished" and result.get("postprocessed") is False:
        # A campaign can finish green and still have nothing derived; saying "finished"
        # alone sends the caller looking for results that were never written.
        #
        # But "postprocessed is false" covers two states that need opposite actions. A step
        # that RAISES leaves the campaign not-postprocessed while the steps beside it already
        # derived and loaded their data, so reporting "nothing was derived" there is false and
        # invites re-running everything.
        error = result.get("postprocessing_error")
        if error:
            return (f"finished, but postprocessing reported an error: {error}. What the "
                    f"steps that DID succeed derived is already loaded and queryable "
                    f"(describe_campaign_data); run_postprocessing re-runs the failed "
                    f"step without re-running any trial")
        return ("finished, but postprocessing did not run: nothing was derived from the "
                "runs yet, so only the campaign's own record (run_view, campaign.*) will "
                "answer. run_postprocessing fixes that without re-running trials")
    return ""


#: Page size used when the whole list has to be walked (``running_only``). The service
#: pages *before* the filter can be applied, so asking for the caller's ``limit`` would
#: filter a window instead of the list — a long-running campaign started last week would
#: drop out of "what is running now" simply for not being among the 20 newest.
_WALK_PAGE = 200


def summary_to_dict(summary) -> dict:
    """Render a service ``CampaignSummary`` into a listing entry.

    ``description`` and ``finished_at`` are omitted when empty rather than reported as
    ``""``/null: a campaign started without a description has none, which is not the same
    fact as "the description is the empty string".

    ``paused`` and ``priority`` are carried the same way -- only when they are not the
    default -- because a held campaign is the one case where no progress is not a fault.
    Without them a campaign somebody parked is indistinguishable here from one that is
    wedged, and the reasonable next move (diagnose it, or start it again) is the wrong one.

    ``mode`` is carried because this listing is the only view an agent has: without it a
    search and a sweep are indistinguishable here, and a search is read with different
    queries (``run_view``'s ``batch``/``objective``/``paramset_id``). ``num_composition_failed``
    and ``num_no_sample`` come along for the same reason — a search whose draws never
    composed, or never scored, has ``num_runs`` telling only part of that.
    """
    entry = {
        "campaign_id": summary.campaign_id,
        "status": summary.phase,
        "mode": summary.mode,
        "started_at": summary.started_at,
        "postprocessed": summary.postprocessed,
        "num_runs": summary.num_runs,
        "num_passed": summary.num_passed,
        "num_failed": summary.num_failed,
        "num_composition_failed": summary.num_composition_failed,
        "num_no_sample": summary.num_no_sample,
    }
    # Omitted when not recorded, like ``finished_at``: a running or unmeasured campaign has
    # no size, which is a different fact from a size of 0.
    if summary.results_bytes is not None:
        entry["results_bytes"] = summary.results_bytes
    if summary.description:
        entry["description"] = summary.description
    if summary.finished_at:
        entry["finished_at"] = summary.finished_at
    if summary.paused:
        entry["paused"] = True
    if summary.priority:
        entry["priority"] = summary.priority
    return entry


def walk_all(client, sort: str, order: str) -> list:
    """Every campaign summary the service knows, in the service's order (live first,
    then by *sort*/*order*).

    Only for ``running_only``. The service leads with the live campaigns, so the first
    page usually holds them all — but nothing bounds their number, so this walks every page.
    """
    from robovast.service.interface import ListCampaignsRequest
    out: list = []
    offset = 0
    while True:
        page = client.list_campaigns(
            ListCampaignsRequest(limit=_WALK_PAGE, offset=offset, sort=sort, order=order))
        out.extend(page.campaigns)
        offset += _WALK_PAGE
        if offset >= page.total or not page.campaigns:
            return out
