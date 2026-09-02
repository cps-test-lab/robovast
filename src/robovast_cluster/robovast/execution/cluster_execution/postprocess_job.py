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

"""In-cluster rosbag→CSV conversion Job — the ROS2 half of analysis postprocessing.

Analysis postprocessing splits along a natural seam: only the ``rosbags_*`` → CSV
step needs ROS2; everything after it (the index ingest, metadata) is plain
Python that runs wherever robovast is installed. Locally ``docker_exec.sh`` runs the
conversion in a container and *bind-mounts* the campaign dir, so outputs appear in
place. A pod cannot bind-mount the caller's filesystem, so in-cluster the conversion
runs as a **Job** and this module builds/creates/tracks it.

Two properties matter:

* **The Job runs the campaign's own execution image** (the system-under-test's
  image, recorded in ``<campaign>/_execution/execution.yaml``) — rosbags carry the
  SUT's *custom ROS2 message types* and only deserialize there. This mirrors local,
  which passes that same image to ``docker_exec.sh --image``.
* **Nothing is baked into that image.** The conversion scripts are *mounted in* via
  an initContainer + emptyDir (the K8s analog of ``-v $SCRIPT_DIR:/scripts:ro``),
  exactly as the run Jobs receive ``entrypoint.sh``/config through a volume. Inputs
  (``/bags``) and outputs (``/out``) are separate dirs — the run-Job pattern — so the
  Job uploads ``/out`` wholesale to a ``<campaign_prefix>_postproc/`` staging prefix
  without ever re-uploading a rosbag.
"""

import hashlib
import json
import logging
import re
import time

from robovast.common.campaign_data import PROBE_DIR
from robovast.common.execution import resolve_sidecar_image

from .kube_client import api_transport_errors
from .node_placement import CAMPAIGN_NODE_TOLERATIONS

logger = logging.getLogger(__name__)

#: Legacy staging prefix. The conversion Job used to mirror its outputs here and the
#: service copied them onward to the canonical paths, so every postprocessed campaign kept
#: a second copy of its derived data under a prefix every reader is told to ignore --
#: storage classified as scratch and retained as canonical. The Job now writes the
#: canonical paths directly and names what it wrote in :data:`OUTPUT_MANIFEST`, which is
#: what the staging prefix was really buying: something cheaper to list than a campaign
#: full of rosbags.
#:
#: Kept because campaigns converted by an older service still have one: :func:`sync_outputs`
#: falls back to it when no manifest is present, and clears it once the data is safely at
#: the canonical paths.
POSTPROC_PREFIX = "_postproc"

#: Campaign-relative path of the manifest the Job writes listing every file it produced,
#: one path per line. Riding inside ``/out`` means the same mirror that carries the outputs
#: carries the index of them, so the two cannot disagree.
#:
#: Built by walking ``/out``, so it describes whatever the Job produced rather than what
#: any one plugin was expected to produce. The payload is whatever declared
#: ``needs_execution_image`` -- the rosbag conversion today, and that flag exists on the
#: plugin base class precisely so it will not always be only that.
#:
#: This is what lets the outputs go straight to the canonical prefix: the service reads one
#: known key and then fetches exactly the objects it names, instead of listing a prefix
#: whose bulk is rosbags it does not want.
OUTPUT_MANIFEST = "_execution/conversion_outputs.txt"

#: Where the staging initContainer leaves its account of itself when it fails.
#:
#: Its own channel, not the step's: a failure here means the step container never starts,
#: so nothing it would have written exists, and a diagnostic that rides the step's
#: end-of-run mirror is a diagnostic that is only delivered when it is not needed. Staging
#: copies the campaign's run data onto the node, so it is also the step that meets a full
#: disk first -- the failure most in need of an explanation is the one that had none.
STAGING_LOG = "_execution/staging.log"
_POLL_SECONDS = 5
_DEFAULT_TIMEOUT = 3 * 60 * 60

#: What the conversion container reserves.
#:
#: **A request, not a tuning knob: without one this pod was invisible to admission.** The
#: budget provider counts the *requests* of every bound pod, so a container that declares none
#: contributes zero -- and this one runs on the same nodes as the trials, deserializing every
#: rosbag of a campaign, while admission believed those cores were free. That is the same
#: class of error the CPU governor work exists to remove: a run's figures becoming a function
#: of what else happened to be on the machine, with nothing downstream able to detect it.
#:
#: The limit is well above the request because the work is bursty and nothing is under test
#: here -- the split is what buys the density, exactly as it does for the simulator.
POSTPROCESS_RESOURCES = {
    "requests": {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "20Gi"},
    "limits": {"cpu": "4", "memory": "8Gi", "ephemeral-storage": "200Gi"},
}

#: The mirror step's own reservation. It is I/O rather than CPU, and it is what writes the
#: campaign's run data into the pod's ``emptyDir`` -- so the ephemeral-storage request
#: matters as much as the CPU one. Without it, a campaign's worth of data lands on whichever
#: node the scheduler picked with nothing having reserved the disk for it, and the node hits
#: disk pressure and evicts the campaign pods running beside it.
#:
#: **Memory is headroom here, not a bound.** The mirror's footprint grows with the number of
#: objects it moves, so no fixed limit is right for every campaign: this one matches the
#: conversion container's, because both are handed the same campaign and there is no reason
#: for the half that fetches the data to be allowed less than the half that reads it. A
#: campaign large enough will still exceed it, and the fix for that is to stage in chunks --
#: peak memory tracking the largest chunk rather than the whole campaign -- not a larger
#: number here.
#:
#: The request stays small against that limit for the reason the conversion's does: the work
#: is bursty, nothing is under test in this pod, and the split is what buys the density.
POSTPROCESS_INIT_RESOURCES = {
    "requests": {"cpu": "250m", "memory": "512Mi", "ephemeral-storage": "20Gi"},
    "limits": {"cpu": "2", "memory": "8Gi", "ephemeral-storage": "200Gi"},
}


def rosbag_commands_for(vast_path: str, skip=None, skip_rosout: bool = False) -> list:
    """The batched ``rosbags_process`` invocations a campaign's ``.vast`` asks for.

    Reuses the same batching the local path uses (``_batch_rosbags_commands`` merges
    every ``rosbags_*`` entry into one ``rosbags_process`` call per ``bag_dir``), so
    the Job runs exactly what ``vast campaign postprocess`` dispatches. Returns a list of
    ``{plugins, bag_dir?, workers?}`` dicts — empty when the campaign configures no
    rosbag conversion (then no Job is needed).
    """
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        _batch_rosbags_commands, get_postprocessing_commands)

    commands = get_postprocessing_commands(vast_path)
    skip_set = set(skip or ())
    if skip_set:
        commands = [
            c for c in commands
            if (c if isinstance(c, str) else list(c.keys())[0]) not in skip_set
        ]
    out = []
    for cmd in _batch_rosbags_commands(commands, skip_rosout=skip_rosout, skip=skip_set):
        if isinstance(cmd, dict) and "rosbags_process" in cmd:
            out.append(cmd["rosbags_process"] or {})
    return out


def campaign_execution_image(campaign_dir) -> str:
    """The image the campaign's runs actually used (its ``_execution/execution.yaml``).

    This is the system-under-test's image — the only place its custom ROS2 message types
    deserialize — and the same source the local path feeds to ``docker_exec.sh --image``.

    The most forgiving of the policies over
    :func:`~robovast.common.campaign_data.campaign_image_record`, and rightly: it wants *a*
    working image to postprocess in, where a re-run wants the bytes the campaign was built
    from. So it prefers the pinned digest — a re-postprocess should deserialize bags against
    the exact image the runs recorded them with, not whatever a floating ``:latest`` resolves
    to now — and falls back to the tag rather than refusing. Raises only when there is neither,
    rather than converting in the wrong image.
    """
    import os  # noqa: PLC0415

    from robovast.common.campaign_data import (campaign_image_record,  # noqa: PLC0415
                                               image_is_pullable)
    from robovast.common.config import SCENARIO_CONTAINER  # noqa: PLC0415

    path = os.path.join(str(campaign_dir), "_execution", "execution.yaml")
    if not os.path.exists(path):
        raise ValueError(f"cannot read the campaign's execution image from {path}: "
                         f"no such file")
    record = campaign_image_record(campaign_dir)
    if image_is_pullable(record.campaign_digest):
        return record.campaign_digest
    scenario = record.role(SCENARIO_CONTAINER)
    image = record.campaign_image or (scenario.declared if scenario else "") or next(
        (r.declared for r in record.roles.values() if r.declared), "")
    if not image:
        raise ValueError(
            f"no execution image recorded in {path}; cannot pick the image whose "
            "custom ROS2 types the rosbags need")
    return str(image)


def publish_execution_dir(cluster_config, campaign_id: str, campaign_root) -> None:
    """Upload a campaign's ``_execution/`` to the store. Raises if the store refuses.

    The POSTPROCESSING section of the campaign log IS ``_execution/postprocessing.log`` in
    the store: `get_campaign_logs` reads the tracked scratch dir first and the store second,
    and a postprocess runs against its own fetched root, which is neither. So the account
    exists nowhere a reader can see it until this has run.

    Callable from here rather than only from the service's tail, because "at the end" is
    too late twice over: a long postprocess shows no section at all while it runs, and a
    failure whose tail cannot reach the store leaves no account anywhere, permanently.
    """
    from pathlib import Path  # noqa: PLC0415

    from . import in_pod_storage  # noqa: PLC0415
    bucket, prefix = in_pod_storage.campaign_storage_location(cluster_config, campaign_id)
    storage = in_pod_storage.storage_client_for(cluster_config)
    storage.upload_dir(str(Path(campaign_root) / "_execution"), bucket, f"{prefix}_execution")


def publish_postprocessing_log(cluster_config, campaign_id: str, campaign_root) -> None:
    """Make the POSTPROCESSING section readable now, best-effort.

    The one file, not the directory: this runs while the postprocess is in progress, and
    the fetched root it publishes from holds the *other* phases' files too, fetched or
    stale, whose live copies are the service's own. Only ``postprocessing.log`` is
    produced here, so only it is this call's to mirror. The tail publish, once the
    postprocess is over and every phase file is final, is the wholesale one.

    Best-effort, including resolving the store: an early read is not worth failing a
    postprocess that is otherwise fine. The tail publish is the one that has to land, and
    it is the caller's.
    """
    from . import in_pod_storage  # noqa: PLC0415
    try:
        bucket, prefix = in_pod_storage.campaign_storage_location(cluster_config,
                                                                  campaign_id)
        storage = in_pod_storage.storage_client_for(cluster_config)
    except Exception as e:  # noqa: BLE001 - an early read is not worth a failed postprocess
        logger.warning("Could not reach the store to publish the postprocessing account "
                       "for %s yet: %s", campaign_id, e)
        return
    in_pod_storage.publish_execution_file(storage, bucket, prefix, campaign_root,
                                          "postprocessing.log")


def sync_outputs(cluster_config, campaign_id: str, campaign_root: str,
                 force: bool = False) -> int:
    """Pull the Job's outputs into *campaign_root*; return the count.

    The Job writes its outputs at campaign-relative paths under the canonical prefix and
    lists them in :data:`OUTPUT_MANIFEST`, so this reads one known key and fetches exactly
    what it names. That is what keeps the fetch cheap without a staging copy: listing the
    campaign prefix would walk every rosbag to find the handful of CSVs beside them, which
    is the cost the old ``_postproc/`` prefix existed to avoid, and it avoided it by
    storing everything twice.

    *force* must be set whenever the conversion **replaced** outputs rather than adding
    them, i.e. whenever it ran with the caches bypassed: a regenerated CSV that keeps its
    byte count is otherwise indistinguishable from the copy already on disk, and the
    campaign root would keep the file the user asked to replace.

    A campaign converted by an older service has no manifest and its outputs sit under the
    legacy prefix, so that case falls back to the prefix fetch. Either way the staging
    prefix is cleared once its contents are safely at the canonical paths -- it is scratch,
    and nothing has ever emptied it.
    """
    from . import in_pod_storage  # noqa: PLC0415

    bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
        cluster_config, campaign_id)
    storage = in_pod_storage.storage_client_for(cluster_config)

    manifest = storage.read_object(bucket, f"{campaign_prefix}{OUTPUT_MANIFEST}")
    if manifest is None:
        n = storage.download_prefix(bucket, f"{campaign_prefix}{POSTPROC_PREFIX}",
                                    campaign_root, force=force)
        logger.info("Synced %d postprocessing output(s) from the legacy staging prefix "
                    "into %s", n, campaign_root)
    else:
        n = _fetch_manifested(storage, bucket, campaign_prefix, campaign_root,
                              manifest, force=force)
        logger.info("Synced %d postprocessing output(s) into %s", n, campaign_root)

    if n:
        _discard_staging(storage, bucket, campaign_prefix, campaign_id)
    return n


def _staging_log_text(cluster_config, campaign_id: str) -> "str | None":
    """The staging container's own log, or ``None`` when it filed none.

    It files one only if it survived long enough to: a pod SIGKILLed by the kubelet under
    node disk pressure, or OOM-killed, never reaches its own report. Staging copies the
    campaign's run data onto the node, so that is the likeliest way it dies -- which is
    why the absence of this log is itself a finding and not simply missing information.
    """
    from . import in_pod_storage  # noqa: PLC0415
    try:
        bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
            cluster_config, campaign_id)
        storage = in_pod_storage.storage_client_for(cluster_config)
        raw = storage.read_object(bucket, f"{campaign_prefix}{STAGING_LOG}")
    except Exception as e:  # noqa: BLE001 - a diagnostic may not raise over the failure
        logger.warning("Could not read the staging log for %s: %s", campaign_id, e)
        return None
    return None if raw is None else raw.decode("utf-8", "replace").rstrip()


def _write_failure_log(cluster_config, campaign_id: str, campaign_root,
                       log_path: str, message: str) -> None:
    """Write the POSTPROCESSING phase file when the conversion produced none.

    The phase file IS the section: every surface assembles the campaign log from the files
    that exist (``campaign_logs.INFRA_PHASES``), so a conversion that wrote nothing left a
    campaign with no POSTPROCESSING section at all -- the reader saw the phases stop after
    RUN, with the failure reported only in a status field elsewhere. Writing the account
    here is what makes a failed postprocess visible where a successful one is read.

    Not an ``add_campaign_log_handler`` around the whole operation, the way the local lane
    can afford: on this lane the same file is written by the conversion Job and pulled down
    by ``sync_outputs``, so a handler streaming into it would be overwritten mid-write by
    the fetch. Only the path where no such file arrived is free to author one.
    """
    import os  # noqa: PLC0415

    staging = _staging_log_text(cluster_config, campaign_id)
    # The message still carries POINTER_SLOT: the pointer is decided after this file is
    # written, precisely BY whether it was. Inside the file the slot has nothing to say --
    # the reader is already in the section it would point at.
    headline = message.replace(POINTER_SLOT, "").strip()
    lines = [
        f"Postprocessing failed: {headline}",
        "",
        "The step container produced no log, so it did not run. The Job stages the "
        "campaign's recorded run data into the pod before postprocessing it; that is "
        "where this failed.",
    ]
    if staging is None:
        lines += [
            "",
            "The staging step filed no log of its own, which does not narrow much: it "
            "files one by uploading it, so a step that failed because the object store "
            "was unreachable cannot, and neither can a container the kubelet killed "
            "outright. The stage named in the line above comes from the pod's exit status "
            "and does not depend on either -- read that, not this absence.",
        ]
    else:
        lines += ["", "===== staging =====", staging]
    text = "\n".join(lines) + "\n"
    logger.warning("Postprocessing failed before its step ran; recording the account "
                   "for %s", campaign_id)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        logger.warning("Could not write the postprocessing log for %s: %s", campaign_id, e)
        return
    if staging is not None:
        try:
            dst = os.path.join(str(campaign_root), STAGING_LOG)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(staging + "\n")
        except OSError as e:
            logger.warning("Could not write the staging log into %s: %s", campaign_root, e)


def _manifest_paths(manifest: bytes) -> list:
    """The campaign-relative paths in a manifest, ignoring anything that escapes it.

    The manifest is written by a container into a location the service then writes to, so
    it is treated as input rather than as instructions: a path that is absolute or reaches
    upwards would have this fetch write outside the campaign root.
    """
    import os  # noqa: PLC0415
    paths = []
    for line in manifest.decode("utf-8", "replace").splitlines():
        rel = line.strip().lstrip("./")
        if not rel or os.path.isabs(rel) or ".." in rel.split("/"):
            continue
        paths.append(rel)
    return paths


def _fetch_manifested(storage, bucket: str, campaign_prefix: str, campaign_root: str,
                      manifest: bytes, force: bool = False) -> int:
    """Fetch exactly the objects the manifest names; return how many were written."""
    import os  # noqa: PLC0415
    n = 0
    for rel in _manifest_paths(manifest):
        dst = os.path.join(campaign_root, rel)
        if not force and os.path.exists(dst):
            # Same rule download_prefix applies, and for the same reason: the durable home
            # is immutable unless the conversion was told to replace what is there.
            size = storage.stat_object(bucket, f"{campaign_prefix}{rel}")
            if size is not None and size == os.path.getsize(dst):
                continue
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if storage.download_object(bucket, f"{campaign_prefix}{rel}", dst):
            n += 1
    return n


def _discard_staging(storage, bucket: str, campaign_prefix: str, campaign_id: str) -> None:
    """Drop a campaign's legacy staging prefix, best-effort.

    Only ever called once the outputs are at the canonical paths, so this removes a second
    copy and never the only one. Best-effort because a staging prefix nobody reads is not
    worth failing a postprocess over -- it is space, not correctness.
    """
    try:
        removed = storage.delete_prefix(bucket, f"{campaign_prefix}{POSTPROC_PREFIX}")
    except Exception as e:  # noqa: BLE001 - reclaiming space may not fail a postprocess
        logger.warning("Could not clear the staging prefix for %s: %s", campaign_id, e)
        return
    if removed:
        logger.info("Cleared %d staged object(s) for %s; the outputs are at their "
                    "canonical paths", removed, campaign_id)


def campaign_vast(campaign_root) -> str:
    """The campaign's ``.vast`` (``<campaign>/_config/<name>.vast``) — the same single
    source of truth the service edits in place, so the cluster conversion Job runs
    exactly the config the re-run dialog saved.
    """
    from robovast.common.results_utils import campaign_vast as _campaign_vast  # noqa: PLC0415

    return str(_campaign_vast(campaign_root))


def postprocess_campaign(cluster_config, campaign_id: str, campaign_root: str,
                         namespace: str, force: bool = False,
                         skip=None, skip_rosout: bool = False,
                         kube_context=None, state=None) -> tuple:
    """Analysis postprocessing for one campaign, in-cluster. Returns ``(ok, message)``.

    The single implementation behind both entry points — the per-campaign controller
    (auto-chain) and the service (explicit re-run):

    1. **conversion Job** — rosbags→CSV in the campaign's own execution image (ROS2);
    2. **sync** its outputs (`_postproc/`) into *campaign_root* — done **regardless
       of the Job's outcome**, so a failed conversion's ``postprocessing.log`` (teed
       and mirrored even on failure) lands in the campaign log the web UI shows;
    3. **stage 2** — index ingest + metadata, pure Python, right here (success only).

    *campaign_root* must already hold the campaign (`_config/`, `campaign.db`,
    `test.xml`s) — true for the controller (it built it) and for the service after
    ``fetch_campaign``.

    *kube_context* must be the same context the campaign's Jobs were submitted with;
    ``None`` means the active kubeconfig context, which is only correct when the caller
    has none of its own.

    *state*, when the caller has one, is where stage 2's step lines are published as the live
    ``stage`` marker (see
    :func:`~robovast.execution.control_server.stage_output_callback`). Stage 1 is deliberately
    **not** covered: it runs in a pod, so its progress reaches this process only when the Job
    ends and its log is synced — the marker therefore stays empty for the conversion and
    starts moving at ``[1/n]``. Reporting stage 1 means tailing the Job's log from
    :func:`run_conversion_job`'s existing poll, which is a separate change.
    """
    rosbag_cmds = rosbag_commands_for(campaign_vast(campaign_root), skip=skip,
                                      skip_rosout=skip_rosout)
    if rosbag_cmds:
        # The same seam the local lane reads, for the same reason: a bag belonging to a job
        # that was stopped by hand or invalidated by the runner cannot be opened, ever, and
        # must not fail the conversion for every job that finished.
        from robovast.results_processing.postprocessing_plugins import (  # noqa: PLC0415
            _interrupted_job_dirs)
        tolerate_under = _interrupted_job_dirs(campaign_root)
        image = campaign_execution_image(campaign_root)
        ok, message = run_conversion_job(
            cluster_config, campaign_id, namespace, image, rosbag_cmds, force=force,
            kube_context=kube_context, tolerate_under=tolerate_under)
        # Sync the Job's outputs regardless of outcome. The conversion tees its
        # stdout/stderr to postprocessing.log and mirrors /out to the object store
        # even on failure, so this lands the POSTPROCESSING section (with the
        # conversion error) in the campaign log the web UI shows and finalize
        # uploads — without it, a failure surfaces only as a terse "kubectl logs"
        # hint the user cannot act on off-cluster.
        # force rides along: it made the Job bypass its caches and REPLACE the CSVs, and
        # the fetch skips same-size files unless told not to.
        sync_outputs(cluster_config, campaign_id, campaign_root, force=force)
        # The conversion's log is now local; get it readable before the host stage, which
        # is the long half. Waiting for the caller's tail is what made a running
        # postprocess show an empty POSTPROCESSING section.
        publish_postprocessing_log(cluster_config, campaign_id, campaign_root)
        if not ok:
            # Echo the conversion error to the service console too. The web UI already
            # has it via the synced postprocessing.log (POSTPROCESSING section); no
            # campaign log handler is attached at this point, so this reaches the
            # ``vast serve`` stdout only — not duplicated into the campaign log.
            #
            # The sync above is also what settles WHERE the message may send the reader:
            # a Job that died before its first ``tee`` mirrored no log, so the section it
            # would name does not exist. Deciding here is the whole point — this is the
            # first place that can tell the two apart.
            import os  # noqa: PLC0415
            log_path = os.path.join(campaign_root, "_execution", "postprocessing.log")
            if os.path.isfile(log_path):
                with open(log_path, encoding="utf-8") as f:
                    logger.warning("Postprocessing conversion failed:\n%s",
                                   f.read().rstrip())
            else:
                _write_failure_log(cluster_config, campaign_id, campaign_root,
                                   log_path, message)
            # Written and published together: this is the whole account of a failure whose
            # Job is reaped 300 s later, so the window in which it can still be published
            # is the one it was written in. The staging log the account quotes needs no
            # publish of its own -- the store is where it was just read from.
            publish_postprocessing_log(cluster_config, campaign_id, campaign_root)
            return False, with_log_pointer(message, log_path)
    else:
        logger.info("Campaign %s configures no rosbag conversion; host steps only",
                    campaign_id)
    import os  # noqa: PLC0415

    from robovast.client.logging_config import add_campaign_log_handler  # noqa: PLC0415
    from robovast.client.logging_config import remove_campaign_log_handler

    # Append the pure-Python host stage's narrative to the same postprocessing.log
    # the conversion Job produced (mode "a"), so both stages form one ordered
    # POSTPROCESSING section. _finalize (auto-chain) / the re-run then upload it to
    # {prefix}_execution/postprocessing.log.
    log_path = os.path.join(str(campaign_root), "_execution", "postprocessing.log")
    handler = None
    try:
        handler = add_campaign_log_handler(log_path)
    except Exception:  # pylint: disable=broad-except
        logger.warning("Could not open postprocessing.log; continuing without it.",
                       exc_info=True)
    try:
        return run_host_postprocessing(
            os.path.dirname(str(campaign_root).rstrip(os.sep)),
            campaign_id, force=force, skip=skip, state=state)
    finally:
        remove_campaign_log_handler(handler)


def run_host_postprocessing(results_dir: str, campaign_id: str, force: bool = False,
                            skip=None, state=None) -> tuple:
    """Stage 2 — everything after the ROS conversion (index ingest, metadata).

    Pure Python, so it runs wherever robovast is installed (the controller pod, the
    service pod). Reuses the *normal* pipeline with the rosbag steps skipped — the
    conversion Job already did those — so there is no second implementation of the
    postprocessing sequence. Returns ``(ok, message)``.

    *state*, when given, also receives each step's line as the live ``stage`` marker — the
    same wiring the local lane uses, so the campaign view narrates this phase identically on
    both. ``None`` leaves it logging only.
    """
    from robovast.execution.control_server import stage_output_callback  # noqa: PLC0415
    from robovast.results_processing.postprocessing import ROSBAG_JOB_NAMES  # noqa: PLC0415
    from robovast.results_processing.postprocessing import run_postprocessing

    return run_postprocessing(
        results_dir=results_dir, campaign=campaign_id, force=force,
        skip=sorted(set(skip or ()) | set(ROSBAG_JOB_NAMES)),
        output_callback=stage_output_callback(state, logger.info))


#: This Job pod runs in a separate context from the controller, so its stdout is
#: otherwise only a transient ``kubectl logs``. Teeing the conversion output to this
#: campaign-relative path lets it ride the wholesale ``/out`` mirror below into the
#: object store, where :func:`sync_outputs` lands it at
#: ``<campaign_root>/_execution/postprocessing.log`` — the POSTPROCESSING section of
#: the unified campaign log (the host stage then appends to the same file).
_POSTPROC_LOG = "/out/_execution/postprocessing.log"

#: Where the conversion records what it produced from what. Under ``/out`` for the same
#: reason the log is: the wholesale mirror below carries it into the object store, and
#: ``sync_outputs`` lands it beside the campaign's other ``_execution`` artifacts, where
#: the host stage picks it up.
#:
#: Without this the Job passed no ``--provenance-file`` at all, so every ``rosbags_*``
#: step produced its tables and recorded nothing -- and the host stage runs with those
#: steps *skipped*, so it had nothing to record either. A cluster campaign's
#: ``postprocessing_steps`` table therefore held one row (``resource_usage``, from the
#: host stage) while four steps had run. The local lane, which does pass one, was
#: unaffected -- so the provenance a campaign carries depended on the lane it ran on.
_ROSBAG_PROVENANCE = "/out/_execution/rosbags_provenance.json"


def _mirror_excludes() -> str:
    """Keep the calibration probes out of the Job's copy of the campaign tree.

    The alternative -- mirror everything and tell the scanner to skip -- was the first fix
    and it is the weaker one. What the Job never receives it cannot convert, cannot fail
    on, and does not pay to download; and the rule lives in the one place that decides what
    this Job is given, rather than in a flag a second lane can forget to pass. That
    forgetting is not hypothetical: ``--tolerate-under`` had exactly this shape and held
    off-cluster only, and the skip list repeated the omission here.

    A probe is deliberately not a run, so its bag is not campaign data. Converting it cost
    a bag's work per node, and an interrupted probe's unfinalized bag failed the whole step
    on something nothing was ever going to read.

    **Only ``_calibration``, not every reserved directory.** The others hold data this Job
    needs: ``_jobs/<batch>/<job>/logs/rosout_bag`` is each job's real log bag, so excluding
    the set wholesale would silently drop every ``/rosout`` record in the campaign. The two
    look interchangeable from their names alone and are not.
    """
    return f"--exclude {_shquote(PROBE_DIR + '/*')} "


#: Name of the container that stages the campaign's run data into the Job's pod.
STAGING_CONTAINER = "s3-init"

#: What each staging exit status means, keyed by the code the script exits with.
#:
#: **An exit code is the one channel that always survives.** It is in the pod's status
#: whatever happened to the container -- no store to reach, no file to write, no cleanup to
#: run -- so a stage that fails while the object store is exactly what is unreachable can
#: still say which stage it was. Uploading a log cannot make that claim: the upload needs
#: the very thing whose absence it would be reporting.
#:
#: Deliberately not 1. A bare ``exited 1`` is every failure at once and names none of them,
#: which is what a staging failure looked like: the container that could not reach the store
#: and the container that ran out of disk were the same single line.
STAGING_EXIT_REASONS = {
    41: "could not install the object-store client into the pod",
    42: "could not reach the object store (its endpoint or credentials)",
    43: "could not stage the campaign's run data into the pod "
        "(node disk is what to check first)",
}


def _staging_script() -> str:
    """The initContainer's shell: stage the campaign's run data, and account for itself.

    Each stage exits with its own code from :data:`STAGING_EXIT_REASONS`, so the pod's
    status alone says which one failed. That is the account that cannot go missing; the
    log below is the detail on top of it, not the mechanism.

    Everything is captured to a file, echoed on, and pushed to :data:`STAGING_LOG` on
    failure. The alias is re-set immediately before that push: the first attempt may itself
    be what failed, and re-trying costs nothing where giving up guarantees silence. It is
    still best-effort -- if the store is genuinely unreachable no upload can work, which is
    exactly why the exit code carries the answer instead.

    Plain ``sh``: no ``pipefail`` and no ``PIPESTATUS`` here, so each stage's status is
    taken directly rather than through a pipe.
    """
    alias = ('mc alias set mystore "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY"')
    return "\n".join([
        "log=/tools/staging.log",
        "rc=0",
        # A SUBSHELL, not a brace group: `exit 43` in a brace group would end the whole
        # script and skip both the echo and the upload below, so the stage code would be
        # the only thing that ever came out. Here it ends the subshell and `|| rc=$?`
        # catches it.
        "(",
        '  cp "$(command -v mc)" /tools/mc && chmod +x /tools/mc || exit 41',
        f"  {alias} || exit 42",
        "  mc mirror " + _mirror_excludes() +
        '"mystore/$S3_BUCKET/$S3_CAMPAIGN_PREFIX" /bags/ || exit 43',
        ') > "$log" 2>&1 || rc=$?',
        # Still on stdout, so a pod that is still around reads the same way it always did.
        'cat "$log"',
        'if [ "$rc" -ne 0 ]; then',
        f"  {alias} >/dev/null 2>&1 || true",
        f'  mc cp "$log" "mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}{STAGING_LOG}" || true',
        "fi",
        'exit "$rc"',
    ])


def _conversion_script(rosbag_cmds: list, force: bool, tolerate_under=()) -> str:
    """The main container's shell: convert each batch, then mirror /out up.

    All setup and conversion stdout/stderr is teed into ``_POSTPROC_LOG`` so it becomes
    the POSTPROCESSING section of the campaign's unified log. ``pipefail`` preserves the
    conversion's exit status through the ``tee`` pipe, and the ``/out`` mirror is an EXIT
    trap so the log (with any error) is uploaded however the script ends -- including the
    setup failures that abort it before the conversion runs at all.
    """
    convert = []
    for params in rosbag_cmds:
        args = [
            "/scripts/ros2_exec.sh", "/scripts/rosbags_process.py",
            "--config", _shquote(json.dumps({"plugins": params.get("plugins", [])})),
            # Outputs go to their own tree (never beside the bags), so the upload
            # below carries only what this Job produced.
            "--output-root", "/out",
            "--provenance-file", _ROSBAG_PROVENANCE,
        ]
        if params.get("bag_dir") is not None:
            args += ["--bag-dir", _shquote(str(params["bag_dir"]))]
        if params.get("workers") is not None:
            args += ["--workers", str(int(params["workers"]))]
        if force:
            args.append("--force")
        # A job an operator stopped by hand (or the runner invalidated) was SIGKILLed
        # mid-write, so its rosbag is unfinalized and can never be opened. Without these
        # the whole campaign's conversion exits non-zero on that one bag, costing the
        # metrics of every job that DID finish -- see `_interrupted_job_dirs`, which is the
        # shared seam for this rule and which the LOCAL lane has always consulted here.
        # This lane built its own arg list and did not, so the rule held off-cluster only.
        for job_dir in tolerate_under:
            args += ["--tolerate-under", _shquote(str(job_dir))]
        args.append("/bags")
        convert.append(" ".join(args))

    # Straight to the canonical prefix. /out holds only what this Job produced, at
    # campaign-relative paths, and `mc mirror` without --remove writes exactly those keys,
    # so there is nothing here a staging copy would have protected the campaign from.
    mirror = '/tools/mc mirror --overwrite /out/ "mystore/$S3_BUCKET/$S3_CAMPAIGN_PREFIX"'
    # Written before the mirror so it rides up with what it describes. `find` runs after the
    # conversion, so it names what was actually produced rather than what was intended.
    manifest = (f'(cd /out && find . -type f | sed "s|^\\./||") > /out/{OUTPUT_MANIFEST} '
                '2>/dev/null || true')
    lines = [
        "set -eo pipefail",
        # The log is created and the setup runs INSIDE it, and the mirror is a trap, so
        # that every way this script can end still leaves an account behind. Setup under
        # `set -e` is exactly where the silent failures live: an unwritable /out or an
        # unreachable object store aborts before the conversion's own `tee`, and a plain
        # trailing mirror is then never reached. The campaign is left pointed at a
        # POSTPROCESSING section that does not exist and cannot be made to exist.
        f"mkdir -p $(dirname {_POSTPROC_LOG}) || exit 1",
        # `|| true`: a failed mirror must not overwrite the conversion's own exit status
        # with its own, and there is nowhere left to report it to anyway.
        f"trap '{manifest}; {mirror} || true' EXIT",
        "rc=0",
        "(",
        # `set -e` ahead of the alias, so a store this Job cannot reach stops it here and
        # says so in the log, rather than letting every conversion run on and fail with an
        # error about bags.
        "  set -e",
        '  /tools/mc alias set mystore "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY"',
        # Run the conversions in the same subshell, teeing all of it to the log.
        "\n".join("  " + c for c in convert),
        f') 2>&1 | tee -a "{_POSTPROC_LOG}" || rc=$?',
        "exit $rc",
    ]
    return "\n".join(lines)


def _shquote(value: str) -> str:
    import shlex  # noqa: PLC0415
    return shlex.quote(value)


def _short_job_name(prefix: str, campaign: str, discriminator: str = "") -> str:
    """Build a Kubernetes Job name ``<prefix><campaign>[-<discriminator>]`` capped at 63.

    Kubernetes copies the Job's ``metadata.name`` verbatim into the pod template's
    ``job-name`` label, and label values may be at most 63 chars — so the *name*
    itself (not just the label-safe campaign) has to fit, otherwise the Job is
    rejected with ``spec.template.labels: ... must be no more than 63 characters``.
    Keep the readable head of the campaign and append a short hash so distinct
    campaigns that share a truncated head still map to distinct Job names.
    """
    # The discriminator says WHICH conversion of this campaign the Job is. A search
    # converts once per repetitions-group, and while the name was the campaign's alone the
    # second create returned 409, fell through to the FIRST conversion's completed Job, and
    # reported success having converted nothing.
    identity = f"{campaign}-{discriminator}" if discriminator else campaign
    safe = re.sub(r"[^a-z0-9.-]", "", identity.lower().replace("_", "-").replace("/", "-"))
    full = f"{prefix}{safe}"
    if len(full) <= 63:
        return full
    # Hashed over the DISCRIMINATED identity, so truncation is not what makes two
    # conversions collide again.
    digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
    head = safe[: 63 - len(prefix) - 1 - len(digest)].rstrip("-.")
    return f"{prefix}{head}-{digest}"


#: Prefix for the per-campaign ConfigMap that carries the conversion scripts.
_SCRIPTS_CM_PREFIX = "robovast-postproc-scripts-"


def _scripts_cm_name(campaign_id: str, discriminator: str = "") -> str:
    """Discriminated with its Job: each conversion deletes this when it finishes, so a
    shared name lets a finishing conversion delete one another is still mounting."""
    return _short_job_name(_SCRIPTS_CM_PREFIX, campaign_id, discriminator)


def scripts_configmap_manifest(campaign_id: str, namespace: str,
                               discriminator: str = "") -> dict:
    """A ConfigMap carrying the *driver's own* conversion scripts.

    Built from ``robovast.results_processing.data`` — the same package dir the local
    path bind-mounts via ``docker_exec.sh -v <scripts>:/scripts``. Mounting this in the
    conversion Job (instead of copying ``/scripts`` from a separately-versioned
    controller image) makes the in-cluster scripts always match the driver that
    generated the conversion command, so the driver/script version skew that produced
    the ``--output-root`` failure cannot occur on any exec variant. The scripts are
    self-contained (stdlib + ROS2 libs + one sibling, no ``robovast`` import) and small
    (well under the 1 MiB ConfigMap limit), so a plain text ConfigMap suffices.
    """
    from importlib.resources import files  # noqa: PLC0415

    from .cluster_execution import _label_safe_campaign  # noqa: PLC0415

    data_dir = files("robovast.results_processing.data")
    payload = {}
    for entry in data_dir.iterdir():
        if entry.name == "__pycache__" or not entry.is_file():
            continue
        payload[entry.name] = entry.read_text(encoding="utf-8")
    if "rosbags_process.py" not in payload:
        raise RuntimeError(
            "conversion scripts not found in robovast.results_processing.data; "
            "cannot build the postprocessing ConfigMap")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _scripts_cm_name(campaign_id, discriminator),
            "namespace": namespace,
            "labels": {"jobgroup": "postprocessing",
                       "campaign-id": _label_safe_campaign(campaign_id)},
        },
        "data": payload,
    }


#: The slot :func:`job_failed_message` leaves for the pointer, filled by
#: :func:`with_log_pointer`. A literal marker rather than string surgery on the message,
#: so that only the ONE message which promises a log gets a pointer: a blocked pod and a
#: timeout carry their own complete explanation and must come through untouched.
POINTER_SLOT = "<<log>>"

#: Appended to a failed Job's message once the conversion log has actually been synced
#: down. Kept apart from :func:`job_failed_message` because only the caller that has run
#: :func:`sync_outputs` knows whether the section it names exists.
LOG_POINTER = ("— see the POSTPROCESSING section of the campaign log for what it "
               "reported")

#: The same slot when no log arrived, and then there is no POSTPROCESSING section and
#: never will be. Pointing at one regardless sends the reader to an empty panel and reads
#: as a second fault on top of the first.
#:
#: The message names the STAGE rather than a cause, because two very different failures
#: land here and the message cannot tell them apart. The conversion container can abort in
#: setup ahead of its first ``tee``; or -- the one that leaves no trace at all -- the
#: ``s3-init`` initContainer can fail while staging the campaign's bags into the pod, in
#: which case the conversion container never starts and there is nothing anywhere to tee.
#: Staging is the likelier of the two on a campaign of any size: it pulls every bag onto
#: the node, so it is the step that meets a full disk first.
NO_LOG_POINTER = ("— before its step produced any output, so the campaign log has no "
                  "POSTPROCESSING section and nothing it reported to read. The step did "
                  "not run: the Job failed while staging the campaign's run data into the "
                  "pod, or while setting up around it. Node disk and the object store are "
                  "what to check.")


def job_failed_message(job_name: str, pod_reason: str = "") -> str:
    """What a failed conversion Job reports to the user, before the log is accounted for.

    *pod_reason*, when the pod could be read, is the one part of this that does not depend
    on a container having survived to explain itself -- see :func:`pod_failure_reason`. It
    leads, because it is the answer: everything after it is where to read more.

    Named so the string has one definition and a test can hold it to its contract: it
    carries **no cluster command**. It lands on ``postprocessing_error``, which the web UI
    renders to someone who has a log panel and no kubeconfig. A ``kubectl logs
    job/<name> -n <ns>`` appended here is unrunnable for that reader, aimed at whichever
    cluster their context happens to name, and points at a Job that
    ``ttlSecondsAfterFinished`` reaps 300 s after it fails -- so by the time most people
    read it, it names nothing that still exists. The conversion output is in the campaign
    log, which every surface already shows.

    Where to look is NOT decided here: this runs before the Job's outputs are synced, so
    it cannot know whether a POSTPROCESSING section exists. The caller appends
    :data:`LOG_POINTER` or :data:`NO_LOG_POINTER` once it does.
    """
    if pod_reason:
        return f"postprocessing job {job_name} failed -- {pod_reason} {POINTER_SLOT}"
    return f"postprocessing job {job_name} failed {POINTER_SLOT}"


def with_log_pointer(message: str, log_path) -> str:
    """Fill :data:`POINTER_SLOT` according to whether the conversion log actually arrived.

    A message without the slot is returned unchanged, which is what keeps a blocked pod's
    or a timeout's own explanation intact.
    """
    import os  # noqa: PLC0415
    if POINTER_SLOT not in message:
        return message
    pointer = LOG_POINTER if os.path.isfile(log_path) else NO_LOG_POINTER
    return message.replace(POINTER_SLOT, pointer)


def _adopt_or_replace(batch, namespace: str, name: str, manifest: dict) -> bool:
    """Keep a still-running Job; delete and re-create a finished one. False if neither held.

    A conversion Job's name comes from the campaign, so the same name is reused every time
    postprocessing is retriggered. Waiting on whatever answers to it means a reaped-but-not-
    yet-gone Job from an earlier attempt reports its outcome as the new attempt's -- the
    retrigger looks like it ran and produces the previous answer.

    ``propagationPolicy=Foreground`` so the delete returns only once the pods are going,
    and the re-create cannot race the corpse of the run it replaces.
    """
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    try:
        existing = batch.read_namespaced_job(name=name, namespace=namespace)
    except ApiException as e:
        # Gone between the create and this read: the name is free, so try once more.
        if e.status == 404:
            try:
                batch.create_namespaced_job(namespace=namespace, body=manifest)
                return True
            except ApiException:
                return False
        return False

    status = getattr(existing, "status", None)
    if getattr(status, "active", None):
        logger.info("Postprocessing job %s is already running; waiting on it", name)
        return True

    logger.info("Replacing finished postprocessing job %s", name)
    try:
        batch.delete_namespaced_job(name=name, namespace=namespace,
                                    propagation_policy="Foreground")
    except ApiException as e:
        if e.status != 404:
            logger.warning("Could not delete %s: %s", name, e)
            return False

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            batch.read_namespaced_job(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                break
            return False
        time.sleep(_POLL_SECONDS)
    else:
        logger.warning("Postprocessing job %s did not go away", name)
        return False

    try:
        batch.create_namespaced_job(namespace=namespace, body=manifest)
    except ApiException as e:
        logger.warning("Could not re-create %s: %s", name, e)
        return False
    return True


def pod_failure_reason(core, namespace: str, job_name: str) -> str:
    """Why this Job's pod actually died, read from the pod rather than from the pod's help.

    The only account that does not depend on the dying container's cooperation. A pod
    SIGKILLed by the kubelet under node disk pressure, or OOM-killed, runs no cleanup and
    files no report -- so a design where the container uploads its own log covers the
    graceful failures and misses the ones that matter most. The kubelet, meanwhile, has
    recorded ``Evicted`` with a message naming ephemeral storage all along.

    Nothing here is specific to what the Job was running. The payload is whatever
    postprocessing plugin declared ``needs_execution_image`` -- today the rosbag
    conversion, deliberately not only that -- and a pod's cause of death is the same
    question either way.

    Infrastructure causes come from :func:`pod_termination_reason`, shared with the run
    loop so both lanes agree on what those mean. A plain non-zero exit is added here, which
    that function deliberately omits: for a *run* the reason is in the scenario's own log,
    but this Job's step may have died before writing one, and then the container's name and
    exit code are the whole of what is known.

    Advisory: this runs while reporting a failure, so it must not raise one of its own.
    """
    from .cluster_execution import pod_termination_reason  # noqa: PLC0415

    try:
        pods = core.list_namespaced_pod(namespace=namespace,
                                        label_selector=f"job-name={job_name}").items or []
    except Exception as e:  # noqa: BLE001 - advisory only
        logger.debug("Could not read pods of %s: %s", job_name, e)
        return ""

    for pod in pods:
        found = pod_termination_reason(pod)
        if found:
            reason, message = found
            return f"{reason}: {message}" if message else reason

    for pod in pods:
        status = getattr(pod, "status", None)
        # Init containers first and in declaration order: staging runs before the step, so
        # when staging is what failed the step container's status says nothing.
        statuses = list(getattr(status, "init_container_statuses", None) or []) + \
            list(getattr(status, "container_statuses", None) or [])
        for cs in statuses:
            state = getattr(cs, "state", None)
            term = getattr(state, "terminated", None) if state else None
            code = getattr(term, "exit_code", None) if term else None
            if isinstance(code, int) and code != 0:
                name = getattr(cs, "name", None) or "?"
                # The staging stage's own vocabulary, where it has one. `exited 1 (Error)`
                # is every failure at once and names none of them; `exited 42` is a code
                # this script chose in order to be read.
                if name == STAGING_CONTAINER and code in STAGING_EXIT_REASONS:
                    return f"container {name} {STAGING_EXIT_REASONS[code]}"
                detail = (getattr(term, "reason", None) or "").strip()
                exited = f"container {name} exited {code}"
                return f"{exited} ({detail})" if detail else exited
    return ""


def _blocked_reason(core, namespace: str, job_name: str) -> str:
    """``"<reason>: <message>"`` when this Job's pod cannot start, else ``""``.

    Reuses the signal the run loop and the image build already act on rather than reading pod
    status again here, so all three agree about what "blocked" means. Advisory: a pod list that
    cannot be read must not turn a running conversion into a reported failure, so an error here
    yields ``""`` and the wait continues to its own deadline.
    """
    from .cluster_execution import blocked_job_reasons  # noqa: PLC0415

    try:
        return blocked_job_reasons(core, namespace, f"job-name={job_name}").get(job_name, "")
    except Exception as e:  # noqa: BLE001 - advisory only
        logger.debug("Could not check whether %s is blocked: %s", job_name, e)
        return ""


def build_manifest(campaign_id: str, image: str, rosbag_cmds: list, s3: tuple,
                   namespace: str, force: bool = False,
                   pull_secret_name: str = "", discriminator: str = "",
                   tolerate_under=()) -> dict:
    """Build the conversion Job manifest.

    Args:
        campaign_id: The campaign to convert.
        image: **The campaign's execution image** (the SUT image from
            ``_execution/execution.yaml``) — required for its custom ROS2 types.
        rosbag_cmds: :func:`rosbag_commands_for` output.
        s3: ``(endpoint, access_key, secret_key, bucket, campaign_prefix)``.
        namespace: Kubernetes namespace.
        force: Bypass the per-rosbag caches.
        tolerate_under: Campaign-relative artifact dirs of jobs that were cut short
            (:func:`~robovast.results_processing.postprocessing_plugins._interrupted_job_dirs`).
            Their bags are unreadable by construction, so the conversion reports them and
            succeeds instead of failing the campaign.
        pull_secret_name: Secret for this pod's OWN image pulls -- the sidecar that mirrors
            the bags, and the campaign's execution image. Missing entirely until a
            private-registry deployment sat in ``ImagePullBackOff`` while the Job stayed
            ``active``, so the wait below reported a timeout and named neither the image nor
            the registry. Same omission the build Job had, in the same direction.

    The ``/scripts`` come from a per-campaign ConfigMap (see
    :func:`scripts_configmap_manifest`) built from the driver's own
    ``results_processing/data`` — the K8s analog of ``docker_exec.sh``'s
    ``-v <scripts>:/scripts`` — so the script version always matches the driver.
    """
    from .cluster_execution import _label_safe_campaign  # noqa: PLC0415

    endpoint, access_key, secret_key, bucket, campaign_prefix = s3
    safe = _label_safe_campaign(campaign_id)
    s3_env = [
        {"name": "S3_ENDPOINT", "value": endpoint},
        {"name": "S3_BUCKET", "value": bucket},
        {"name": "S3_ACCESS_KEY", "value": access_key},
        {"name": "S3_SECRET_KEY", "value": secret_key},
        {"name": "S3_CAMPAIGN_PREFIX", "value": campaign_prefix},
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": _short_job_name("robovast-postproc-", campaign_id, discriminator),
            "namespace": namespace,
            "labels": {
                "jobgroup": "postprocessing",
                "campaign-id": safe,
            },
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {
                    "labels": {"jobgroup": "postprocessing", "campaign-id": safe},
                },
                "spec": {
                    "restartPolicy": "Never",
                    # Campaign nodes are where the bags already are and where this is
                    # allowed to run; without the toleration a deployment that dedicates
                    # its nodes to campaigns has nowhere to put this at all, and the Job
                    # sits Pending until its three-hour timeout. Easy to miss here, because
                    # this Job is created outside the admission path entirely.
                    "tolerations": list(CAMPAIGN_NODE_TOLERATIONS),
                    **({"imagePullSecrets": [{"name": pull_secret_name}]}
                       if pull_secret_name else {}),
                    "volumes": [
                        # The driver's own conversion scripts, executable (0755).
                        {"name": "scripts",
                         "configMap": {"name": _scripts_cm_name(campaign_id,
                                                                    discriminator),
                                       "defaultMode": 0o755}},
                        {"name": "tools", "emptyDir": {}},
                        {"name": "bags", "emptyDir": {}},
                        {"name": "out", "emptyDir": {}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                    "initContainers": [
                        {
                            # mc for the upload + mirror the campaign (rosbags) down.
                            # (The scripts are not copied by an initContainer — they
                            # arrive read-only from the ConfigMap volume above.)
                            "name": "s3-init",
                            "image": resolve_sidecar_image(),
                            "command": ["sh", "-c", _staging_script()],
                            "env": s3_env,
                            "volumeMounts": [
                                {"name": "tools", "mountPath": "/tools"},
                                {"name": "bags", "mountPath": "/bags"},
                            ],
                            "resources": POSTPROCESS_INIT_RESOURCES,
                        },
                    ],
                    "containers": [{
                        "name": "convert",
                        # The system-under-test's own image: custom ROS2 types only
                        # deserialize here. ros2_exec.sh sources /opt/ros + /ws/install.
                        "image": image,
                        "command": ["/bin/bash", "-c",
                                    _conversion_script(rosbag_cmds, force, tolerate_under)],
                        "env": s3_env,
                        "volumeMounts": [
                            {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
                            {"name": "tools", "mountPath": "/tools", "readOnly": True},
                            {"name": "bags", "mountPath": "/bags"},
                            {"name": "out", "mountPath": "/out"},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                        "resources": POSTPROCESS_RESOURCES,
                    }],
                },
            },
        },
    }


def run_conversion_job(cluster_config, campaign_id: str, namespace: str, image: str,
                       rosbag_cmds: list, force: bool = False,
                       timeout: int = _DEFAULT_TIMEOUT, kube_context=None,
                       discriminator: str = "", tolerate_under=()) -> tuple:
    """Create the conversion Job and wait for it. Returns ``(ok, message)``.

    A no-op success when the campaign configures no rosbag conversion.

    *discriminator* names WHICH conversion of this campaign this is, and must be set by
    any caller that converts the same campaign more than once -- a search, which converts
    once per repetitions-group. Without it the second create returns 409 and the wait below
    reads the FIRST conversion's already-completed Job, returning "rosbag conversion
    complete" having converted nothing. Left empty the Job keeps the one-shot
    campaign-level name, where the 409 fallthrough is right: a retry of a single conversion
    should wait on the Job already in flight rather than launch a second copy.
    """
    if not rosbag_cmds:
        return True, "no rosbag conversion configured; nothing to run"

    from kubernetes import client  # noqa: PLC0415
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    from . import in_pod_storage  # noqa: PLC0415

    bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
        cluster_config, campaign_id)
    access_key, secret_key = cluster_config.get_s3_credentials()
    s3 = (cluster_config.get_s3_endpoint(), access_key, secret_key, bucket, campaign_prefix)

    from robovast.common.errors import ClusterUnreachableError  # noqa: PLC0415

    from .cluster_execution import resolve_pull_secret  # noqa: PLC0415
    from .kube_client import load_kube_config  # noqa: PLC0415

    # Explicitly, and it must stay explicit: these clients read whatever context is loaded
    # when they are constructed. Without this load, postprocessing dials the ambient
    # kubeconfig while the campaign's Jobs went to the service's --context cluster -- failing
    # against a cluster the campaign never used, and naming the configured API server as
    # unreachable while quoting a timeout to a different address.
    load_kube_config(kube_context)
    core = client.CoreV1Api()
    batch = client.BatchV1Api()
    manifest = build_manifest(
        campaign_id, image, rosbag_cmds, s3, namespace, force=force,
        pull_secret_name=resolve_pull_secret(cluster_config, core, namespace),
        discriminator=discriminator, tolerate_under=tolerate_under)
    name = manifest["metadata"]["name"]

    # The conversion scripts arrive as a per-campaign ConfigMap mounted at /scripts —
    # the driver's own copy, so no controller-image version skew. Create it before the
    # Job (the pod waits in ContainerCreating until the volume source exists) and delete
    # it once the Job is done.
    cm = scripts_configmap_manifest(campaign_id, namespace, discriminator=discriminator)
    cm_name = cm["metadata"]["name"]
    # First call that actually touches the API server, so it is where an unreachable
    # cluster surfaces. Reported as a reason on the campaign's postprocessing_error and
    # re-runnable once the cluster is back -- the runs themselves are already published --
    # rather than reaching the caller as a urllib3 traceback.
    try:
        with api_transport_errors("submitting the postprocessing job"):
            try:
                core.create_namespaced_config_map(namespace=namespace, body=cm)
            except ApiException as e:
                if e.status == 409:  # a stale copy from a prior run — replace it
                    core.replace_namespaced_config_map(name=cm_name, namespace=namespace,
                                                       body=cm)
                else:
                    return False, f"could not create postprocessing scripts ConfigMap: {e}"
    except ClusterUnreachableError as e:
        return False, f"postprocessing cannot be scheduled: {e}"

    try:
        try:
            batch.create_namespaced_job(namespace=namespace, body=manifest)
        except ApiException as e:
            if e.status != 409:
                return False, f"could not create postprocessing job: {e}"
            # 409 means a Job of this name is already here, and the name is derived from
            # the campaign -- so a FINISHED one from an earlier attempt is indistinguishable
            # from a running one until it is read. Falling through to wait on it reported
            # that earlier attempt's outcome as this attempt's: a retrigger that changed
            # nothing, against a pod whose containers ran a previous version of this
            # script, presented as a fresh result. Adopt a live one; replace a finished one.
            if not _adopt_or_replace(batch, namespace, name, manifest):
                return False, (f"postprocessing job {name} already exists and could not be "
                               f"replaced; retry once it has been removed")
        logger.info("Postprocessing job %s created (image=%s)", name, image)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = batch.read_namespaced_job_status(
                    name=name, namespace=namespace).status
            except ApiException as e:
                if e.status == 404:  # reaped (ttl) — treat as finished
                    return True, "postprocessing job finished (reaped)"
                return False, f"could not read postprocessing job status: {e}"
            if not status.active:
                if status.succeeded:
                    logger.info("Postprocessing job %s succeeded", name)
                    return True, "rosbag conversion complete"
                if status.failed:
                    # Read before the message is built: ttlSecondsAfterFinished reaps this
                    # Job 300 s after it fails, and by the time anyone reads the campaign
                    # the pod that knows why is gone.
                    return False, job_failed_message(
                        name, pod_reason=pod_failure_reason(core, namespace, name))
            # A pod that CANNOT start leaves the Job `active` forever, so the polling above
            # never sees a verdict and this returns "timed out" -- naming a duration where the
            # cause was an unpullable image or an unschedulable pod. The same signal the run
            # loop and the image build already act on.
            blocked = _blocked_reason(core, namespace, name)
            if blocked:
                return False, (
                    f"postprocessing job {name} cannot start: {blocked}. The conversion runs "
                    f"in the campaign's own execution image and mirrors its bags with the "
                    f"sidecar, so this is about pulling or scheduling those -- not about the "
                    f"conversion, which has not run. Nothing about the campaign's results is "
                    f"wrong; re-run postprocessing once the pod can start.")
            time.sleep(_POLL_SECONDS)
        return False, f"postprocessing job {name} timed out after {timeout}s"
    finally:
        # Best-effort cleanup of the scripts ConfigMap (labeled for manual sweep if the
        # driver dies before this runs). The Job's own ttlSecondsAfterFinished reaps it.
        try:
            core.delete_namespaced_config_map(name=cm_name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Could not delete scripts ConfigMap %s: %s", cm_name, e)
