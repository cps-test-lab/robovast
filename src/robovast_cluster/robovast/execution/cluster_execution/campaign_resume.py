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

"""Picking a campaign back up after the service process that drove it went away.

A cluster campaign's compute is Kubernetes Jobs. They are not children of the service
process, they carry no owner reference, and they upload their own results to the object
store — so a pod replacement (``vast service upgrade``, an eviction, a drain, an OOM)
takes away the *driver* and nothing else. What was lost is an in-memory campaign entry, a
thread, and the batch loop's position.

This module restores those, and does it by **re-launching the campaign under its own id**
rather than by a resume path of its own. Everything that makes that safe is a property
elsewhere, each true of a campaign starting now as much as one being re-entered:

* the campaign's records are published when they are written, not at the end, so there is
  something to re-launch from (``_publish_campaign_records`` / ``publish_records``);
* the batch runner plans against the campaign root it is given, so finished jobs are
  adopted instead of re-run (``BatchJobRunner._jobs_already_done``);
* ``create_campaign`` is idempotent by name, so the restored store re-opens its row;
* ``WorkspaceTarget.campaign_id`` names the campaign to adopt.

So there is no resume mode in the launch path, the batch loop or the controller. What is
here is only the part that genuinely has no counterpart in a fresh launch: finding the
campaigns that are owed work, and deciding whether each one can be picked up.

**Discovery has one source**: the object store's campaign index, minus everything with a
terminal ``_execution/outcome.json``. Listing live Jobs would be a second source for the
same set — a campaign with live Jobs is indexed and has no outcome — and two sources of
one truth is one more way for them to disagree.

**Refusals are left alone rather than failed.** A campaign this module will not pick up
keeps whatever ``reconstruct_status_from_disk`` says about it, which is ``crashed``: the
honest answer for a campaign nothing is driving. Its data stays recoverable by hand
through ``import_campaign``.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Why a campaign was not picked up, keyed by campaign id. Reported, not raised: a service
#: must start whatever it finds in the store.
Refusal = str


def _terminal_outcome(service, campaign_id: str) -> bool:
    """Whether the store holds a terminal ``outcome.json`` for *campaign_id*.

    A campaign that recorded an ending is over, however it ended, and picking it up again
    would restart work its own record says is finished. Read as a single ``stat_object``
    rather than by fetching the object: the question is whether the ending exists, and the
    campaign's phase is reconstructed from the file itself later by the ordinary readers.
    """
    from . import in_pod_storage
    cfg = service._cluster_config()  # noqa: SLF001 - same package, one collaborator
    bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
    storage = in_pod_storage.storage_client_for(cfg, interactive=True)
    return storage.stat_object(bucket, f"{prefix}_execution/outcome.json") is not None


def owed_work(service) -> list:
    """Campaign ids the store lists and that recorded no ending, newest first.

    Newest first because that is the order they should be re-launched in: a service coming
    back has to get the campaign someone is watching moving again before it works through
    older ones.
    """
    index = service._campaign_index()  # noqa: SLF001 - same package, one collaborator
    owed = []
    for campaign_id, created_at in sorted(index.items(), key=lambda kv: kv[1] or "",
                                          reverse=True):
        try:
            if not _terminal_outcome(service, campaign_id):
                owed.append(campaign_id)
        except Exception as e:  # noqa: BLE001 - one unreadable campaign is not a failed start
            logger.debug("could not tell whether %s is over: %s", campaign_id, e)
    return owed


def plan_for(service, campaign_id: str, campaign_root: Path):
    """What it takes to re-launch *campaign_id*, or a refusal saying what is missing.

    Returns ``(target, request, None)`` or ``(None, None, refusal)``. The campaign root is
    expected to be restored already — this reads it, and reads nothing from the store.
    """
    from robovast.common.campaign_data import (CampaignImageUnpinnable, campaign_pinned_images,
                                               read_launch_record)
    from robovast.common.common import load_config
    from robovast.common.config import validate_config
    from robovast.common.results_utils import campaign_vast
    from robovast.service.interface import CreateCampaignRequest
    from robovast.service.local_transport import WorkspaceTarget

    launch = read_launch_record(campaign_root)
    if launch is None:
        return None, None, (
            "it recorded no launch.yaml, so what it was asked to run is unknown. A campaign "
            "launched before the service published that record cannot be picked up; its "
            "results are still recoverable with 'vast campaign import'.")
    try:
        vast_path = campaign_vast(campaign_root)
    except ValueError as e:
        return None, None, (
            f"it froze no configuration under _config/, so there is nothing to run ({e}).")

    # Loaded exactly as written, with no migration: a campaign already in flight has to keep
    # running the configuration its finished jobs ran. Bringing it forward mid-campaign would
    # make the second half a different experiment from the first, which is the one thing a
    # resumed campaign must not quietly become. So a config this service cannot read as-is is
    # a refusal rather than a migration -- unlike a retrigger, which is starting over and may
    # legitimately move the config forward first.
    try:
        campaign_config = validate_config(load_config(str(vast_path)))
    except ValueError as e:
        return None, None, (
            f"its frozen configuration is not one this service can run unchanged ({e}). "
            f"Resuming it would mean migrating the config mid-campaign, which would make its "
            f"second half a different experiment from its first.")
    if campaign_config.search is not None:
        return None, None, (
            "it is a search, and a search's proposer state is not recorded anywhere a fresh "
            "process could read it. Resuming without it would skip evaluations the strategy "
            "believes it made; its batches so far are recoverable with 'vast campaign "
            "import'.")

    try:
        pinned = campaign_pinned_images(campaign_root)
    except CampaignImageUnpinnable as e:
        return None, None, (
            f"its images cannot be pinned, so a second half of the campaign might not run "
            f"the bytes the first half did: {e}")

    request = CreateCampaignRequest(
        workspace_id="", config_path=str(vast_path),
        config_filter=launch.get("config_filter") or "",
        campaign_name=launch.get("campaign_name") or "",
        runs=launch.get("runs") or 0,
        postprocess=launch.get("postprocess", True),
        upload_to_share=launch.get("upload_to_share", False),
        show_gui=False,
        description=_description(campaign_root))
    target = WorkspaceTarget(config_path=str(vast_path), campaign_id=campaign_id,
                             pinned_images=pinned or None)
    return target, request, None


def _description(campaign_root: Path) -> str:
    """The campaign's own description, from the store row it published at launch.

    Not from ``launch.yaml``, which deliberately records only the fields a replay needs.
    Empty when the row cannot be read: a re-launch losing the description would be a small
    thing, but silently inventing one would not be.
    """
    from robovast.common.store import STORE_FILENAME, CampaignStore
    db = Path(campaign_root) / STORE_FILENAME
    if not db.is_file():
        return ""
    try:
        store = CampaignStore(db)
        row = store._conn.execute(  # noqa: SLF001 - no narrower reader exists
            "SELECT description FROM campaign ORDER BY id LIMIT 1").fetchone()
        return (row[0] if row else "") or ""
    except Exception as e:  # noqa: BLE001 - a description is not worth refusing a resume
        logger.debug("could not read the description of %s: %s", campaign_root, e)
        return ""


def resume_all(service) -> dict:
    """Re-launch every campaign this store says is still owed work.

    Returns ``{campaign_id: None | refusal}`` — ``None`` where the campaign was picked up.
    Never raises: a service has to start even when the store is unreachable, and a campaign
    it could not pick up is reported rather than lost (its Jobs keep running and its results
    keep landing in the store either way).
    """
    outcomes: dict = {}
    try:
        candidates = owed_work(service)
    except Exception as e:  # noqa: BLE001 - an unreachable store must not block startup
        logger.warning("Could not check for campaigns to resume: %s", e)
        return outcomes
    for campaign_id in candidates:
        try:
            outcomes[campaign_id] = _resume_one(service, campaign_id)
        except Exception as e:  # noqa: BLE001 - one campaign must not stop the others
            logger.warning("Could not resume campaign %s: %s", campaign_id, e,
                           exc_info=True)
            outcomes[campaign_id] = str(e)
    return outcomes


def _resume_one(service, campaign_id: str) -> "str | None":
    """Restore one campaign's root and re-launch it; return a refusal, or ``None``."""
    campaign_root = service._campaign_dir(campaign_id)  # noqa: SLF001
    # The whole prefix, into the driver's own root rather than the scratch cache: from here
    # the controller and the batch runner read it as the campaign's working directory, which
    # is what lets them adopt the finished jobs instead of re-running them.
    service.fetch_campaign(campaign_id, dest=campaign_root)
    target, request, refusal = plan_for(service, campaign_id, Path(campaign_root))
    if refusal is not None:
        logger.info("Not resuming campaign %s: %s", campaign_id, refusal)
        return refusal
    service._launch_campaign(request, target)  # noqa: SLF001
    logger.info("Resumed campaign %s after a service restart", campaign_id)
    return None
