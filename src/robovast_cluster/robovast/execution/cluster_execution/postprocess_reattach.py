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

"""Finding the postprocessing Jobs a service restart left running, so someone records them.

A campaign's postprocessing is a Kubernetes Job that deliberately outlives the service
process (``ClusterService._shutdown_running_campaigns``). Only the waiting process writes the
campaign's postprocessing verdict, so a restart mid-postprocess leaves a Job that runs to
completion and a campaign whose record still carries whatever the *previous* attempt said —
a conversion of every rosbag in the campaign, finished, against a campaign marked as
carrying no derived data. The work is then redone by hand.

:mod:`.campaign_resume` does not cover this, and must not be widened to: it re-launches
campaigns that recorded no ending, and a retriggered postprocess runs on a campaign whose
``_execution/outcome.json`` is terminal — precisely what that module excludes so that a
finished campaign is never restarted. So re-attaching is its own concern, keyed on live
Jobs rather than on owed work.

**Discovery is a labelled listing**, one call: the Job carries
``jobgroup=postprocessing`` and its campaign's label-safe id. Asking the cluster what is
running is the only source that answers for a campaign this process has never heard of and
has no reason to guess at.

**The label is resolved, not trusted as an id.** It is the sanitized campaign id, so the
campaign directories on the service's results volume are the vocabulary it is matched
against, and the Job's name must equal the campaign-level name
(``postprocess_job.campaign_job_name``) — a *discriminated* Job is a search's per-batch
conversion, owed to its batch's driver and to no campaign record at all — or be a part of
the split the campaign recorded (``postprocess_parts.read_plan``). A split is resumed
rather than waited for: the postprocess is started again with the options it recorded,
waits for the parts still running, and then completes the campaign.
"""

import logging
import threading

logger = logging.getLogger(__name__)


def _campaign_ids(service) -> list:
    """Every campaign directory on the service's results volume, by name.

    The tree is the campaign index: a campaign is a directory under the results root whose
    name has a campaign's shape (:func:`~robovast.common.execution.is_campaign_dir`), and
    nothing else has to be up for this to answer.
    """
    from robovast.common.execution import is_campaign_dir  # noqa: PLC0415

    root = service._campaigns_root()  # noqa: SLF001 - same package
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    return sorted(e.name for e in entries if e.is_dir() and is_campaign_dir(e.name))


def _live_by_campaign(service) -> dict:
    """``{campaign_id: [live postprocessing job name, ...]}`` for campaigns held here."""
    from . import postprocess_job
    from .cluster_execution import _label_safe_campaign  # noqa: PLC2701 - same package

    live = postprocess_job.live_campaign_jobs(service.namespace,
                                              kube_context=service.kube_context)
    if not live:
        return {}
    return {campaign_id: live[_label_safe_campaign(campaign_id)]
            for campaign_id in _campaign_ids(service)
            if _label_safe_campaign(campaign_id) in live}


def live_campaign_postprocessing(service) -> dict:
    """``{campaign_id: job name}`` for the campaign-level postprocessing Jobs still active.

    Empty when the cluster cannot be read: that is not a verdict about any campaign, and
    this only ever adds waiters.
    """
    from . import postprocess_job

    found = {}
    for campaign_id, names in _live_by_campaign(service).items():
        job_name = postprocess_job.campaign_job_name(campaign_id)
        if job_name in names:
            found[campaign_id] = job_name
    return found


def live_split_postprocessing(service) -> dict:
    """``{campaign_id: recorded plan}`` for split postprocesses with a part still running."""
    from .postprocess_parts import Part, read_plan, part_job_names

    found = {}
    for campaign_id, names in _live_by_campaign(service).items():
        plan = read_plan(str(service.campaign_dir(campaign_id)))
        if plan is None:
            continue
        parts = [Part(name=n) for n in plan["parts"]]
        if set(part_job_names(campaign_id, parts)) & set(names):
            found[campaign_id] = plan
    return found


def start_reattach(service) -> "threading.Thread | None":
    """Re-attach in the background.

    Off the startup path, because listing the cluster's Jobs can take a while against an
    API server that is itself coming back, and a service that does not answer is worse
    than a verdict that arrives late. Returns the thread so a caller that needs the result
    -- a test -- can join it; nothing else does.
    """
    thread = threading.Thread(target=reattach_all, args=(service,),
                              name="robovast-postproc-reattach", daemon=True)
    thread.start()
    return thread


def reattach_all(service) -> dict:
    """Re-attach to every live postprocessing Job. ``{campaign_id: job name}``.

    Never raises: a service must come up whether or not it could find what a previous
    process left running, and a Job nobody re-attached to still finishes and still delivers
    what it produced — only its verdict is lost, which is the same position as before.
    """
    try:
        live = live_campaign_postprocessing(service)
    except Exception as e:  # noqa: BLE001 - startup outweighs any one campaign's record
        logger.warning("Could not check for postprocessing jobs left running by a previous "
                       "service process, so none is being waited on: %s", e, exc_info=True)
        return {}
    try:
        splits = live_split_postprocessing(service)
    except Exception as e:  # noqa: BLE001 - as above, and it must not cost the waiters
        logger.warning("Could not check for split postprocessing left running by a previous "
                       "service process, so none is being resumed: %s", e, exc_info=True)
        splits = {}
    attached = {}
    for campaign_id, job_name in live.items():
        try:
            if service.reattach_postprocessing(campaign_id, job_name):
                attached[campaign_id] = job_name
        except Exception as e:  # noqa: BLE001 - one campaign must not stop the others
            logger.warning("Could not re-attach to postprocessing job %s for campaign %s: "
                           "%s", job_name, campaign_id, e, exc_info=True)
    for campaign_id, plan in splits.items():
        if campaign_id in attached:
            continue
        try:
            if service.resume_postprocessing(campaign_id, force=plan.get("force", False),
                                             skip=plan.get("skip") or ()):
                attached[campaign_id] = f"{len(plan['parts'])} part(s)"
        except Exception as e:  # noqa: BLE001 - one campaign must not stop the others
            logger.warning("Could not resume the split postprocessing of campaign %s: %s",
                           campaign_id, e, exc_info=True)
    if attached:
        logger.info("Re-attached to %d postprocessing job(s) still running from a previous "
                    "service process: %s", len(attached), ", ".join(sorted(attached)))
    return attached
