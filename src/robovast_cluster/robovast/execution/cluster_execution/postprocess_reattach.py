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
process (``ClusterService._adopts_on_restart``). Only the waiting process writes the
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
has no reason to guess at, and it costs the same whatever the store holds.

**The label is resolved, not trusted as an id.** It is the sanitized campaign id, so the
campaigns the store lists are the vocabulary it is matched against, and the Job's name must
equal the campaign-level name (``postprocess_job.campaign_job_name``) — a *discriminated*
Job is a search's per-batch conversion, owed to its batch's driver and to no campaign
record at all.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: How long to keep asking for the campaign index before giving up on re-attaching, and how
#: long to wait between attempts. A restart is exactly when the store may not be up yet: a
#: redeploy brings the object store and this service back together, and the index read that
#: identifies which campaign a live Job belongs to is then refused for the first seconds of
#: this process's life. Without the wait the discovery finds Jobs, resolves none of them to
#: a campaign, and reports nothing -- inert in the one situation it exists for.
_STORE_PATIENCE_S = 180.0
_STORE_RETRY_S = 5.0


def live_campaign_postprocessing(service) -> dict:
    """``{campaign_id: job name}`` for the campaign-level postprocessing Jobs still active.

    Empty when the cluster or the store cannot be read: neither absence is a verdict about
    any campaign, and this only ever adds waiters.
    """
    from . import postprocess_job
    from .cluster_execution import _label_safe_campaign  # noqa: PLC2701 - same package

    live = postprocess_job.live_campaign_jobs(service.namespace,
                                              kube_context=service.kube_context)
    if not live:
        return {}
    created, _finished = service._campaign_index()  # noqa: SLF001 - same package
    found = {}
    for campaign_id in created:
        job_name = live.get(_label_safe_campaign(campaign_id))
        if job_name and job_name == postprocess_job.campaign_job_name(campaign_id):
            found[campaign_id] = job_name
    return found


def start_reattach(service) -> "threading.Thread | None":
    """Re-attach in the background, waiting for the store if it is not up yet.

    Off the startup path, because the wait below can take minutes and a service that does
    not answer is worse than a verdict that arrives late. Returns the thread so a caller
    that needs the result -- a test -- can join it; nothing else does.
    """
    thread = threading.Thread(target=reattach_all, args=(service,),
                              name="robovast-postproc-reattach", daemon=True)
    thread.start()
    return thread


def _live_when_the_store_answers(service, deadline: float) -> dict:
    """The live Jobs, retrying while the cluster names some that the store cannot place.

    That combination -- Jobs running, no campaign to attribute them to -- is what a store
    which has not finished starting looks like, and it is the only case worth waiting on. A
    cluster naming no Jobs is a finished answer, and so is one whose Jobs all resolve.
    """
    from . import postprocess_job

    while True:
        live = live_campaign_postprocessing(service)
        if live:
            return live
        if not postprocess_job.live_campaign_jobs(service.namespace,
                                                  kube_context=service.kube_context):
            return {}
        if time.monotonic() >= deadline:
            logger.warning(
                "Postprocessing jobs are running that this service could not attribute to "
                "a campaign, because the campaign index stayed unreadable. Their verdicts "
                "will not be recorded; re-run postprocessing for those campaigns once the "
                "store is reachable.")
            return {}
        logger.info("Postprocessing jobs are running but the campaign index cannot be read "
                    "yet; waiting for the store before re-attaching.")
        time.sleep(_STORE_RETRY_S)


def reattach_all(service, patience_s: float = _STORE_PATIENCE_S) -> dict:
    """Re-attach to every live postprocessing Job. ``{campaign_id: job name}``.

    Never raises: a service must come up whether or not it could find what a previous
    process left running, and a Job nobody re-attached to still finishes and still uploads
    what it produced — only its verdict is lost, which is the same position as before.
    """
    try:
        live = _live_when_the_store_answers(service, time.monotonic() + patience_s)
    except Exception as e:  # noqa: BLE001 - startup outweighs any one campaign's record
        logger.warning("Could not check for postprocessing jobs left running by a previous "
                       "service process, so none is being waited on: %s", e, exc_info=True)
        return {}
    attached = {}
    for campaign_id, job_name in live.items():
        try:
            if service.reattach_postprocessing(campaign_id, job_name):
                attached[campaign_id] = job_name
        except Exception as e:  # noqa: BLE001 - one campaign must not stop the others
            logger.warning("Could not re-attach to postprocessing job %s for campaign %s: "
                           "%s", job_name, campaign_id, e, exc_info=True)
    if attached:
        logger.info("Re-attached to %d postprocessing job(s) still running from a previous "
                    "service process: %s", len(attached), ", ".join(sorted(attached)))
    return attached
