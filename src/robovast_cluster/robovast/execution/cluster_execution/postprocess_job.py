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

"""In-cluster analysis postprocessing Job — the whole of it, in one pod.

Locally ``docker_exec.sh`` runs the ROS2 conversion in a container and *bind-mounts*
the campaign dir, so outputs appear in place and the pure-Python half runs beside it.
A pod cannot bind-mount the caller's filesystem, so in-cluster postprocessing runs as
a **Job** and this module builds/creates/tracks it.

One pod, one copy of the data. A ``campaign`` ``emptyDir`` mounted at
:data:`CAMPAIGN_MOUNT` in every container holds the campaign tree; the containers are
ordered by Kubernetes alone, because initContainers run sequentially to completion in
declaration order before the regular containers start:

* ``stage`` (initContainer, controller image) fetches the campaign's recorded run data
  into the shared mount.
* ``convert`` (initContainer, the campaign's execution image) runs the ``rosbags_*`` →
  CSV step, reading and writing that mount only. It exists **only when the campaign
  declares a plugin needing the execution image** — rosbags carry the system under
  test's *custom ROS2 message types* and only deserialize in its image, which is also
  why nothing else may be asked of that image.
* ``host`` (container, controller image) runs everything after the conversion — the
  derived tables, and for a campaign-level Job the index ingest and metadata — and is what
  uploads the results. It is the pod's main container in both shapes of this Job, because
  it is the only one given the store: a Job that ended at its conversion could send
  nothing anywhere. A per-batch Job (a search's) runs it without the completing steps.

**Nothing is baked into the execution image.** The conversion scripts are mounted in
from a per-campaign ConfigMap (the K8s analog of ``-v $SCRIPT_DIR:/scripts:ro``), so
the scripts always match the driver that generated the command.
"""

import datetime
import copy
import hashlib
import json
import logging
import re
import time

from robovast.common.execution import resolve_controller_image
from robovast.common.quantity import to_bytes, to_cores

from .kube_client import api_transport_errors
from . import postprocess_usage
from .node_placement import (CAMPAIGN_NODE_TOLERATIONS, NODE_ID_LABEL,
                             job_node_pool)

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

#: ``jobgroup`` label every postprocessing Job carries. Named rather than repeated because
#: it is a contract in two directions: the manifest sets it and :func:`live_campaign_jobs`
#: selects on it, and a Job created without it is a Job no later service process can find
#: still running.
POSTPROCESS_JOBGROUP = "postprocessing"

#: Campaign-relative path of the manifest listing every file the Job uploaded, one path
#: per line. Riding inside the campaign tree means whatever carries the outputs carries the
#: index of them, so the two cannot disagree.
#:
#: Written by the ``host`` container as it uploads (:func:`~.postprocess_host._upload_derived`),
#: which is the only place that knows what went up. Describes whatever the Job produced
#: rather than what any one plugin was expected to produce.
#:
#: This is what lets the outputs go straight to the canonical prefix: the service reads one
#: known key and then fetches exactly the objects it names, instead of listing a prefix
#: whose bulk is rosbags it does not want.
#:
#: An ABSENT manifest is not an empty one. Absent means no Job ever said, and the fetch
#: falls back to the legacy prefix for a campaign converted by an older service; empty means
#: a Job ran and derived nothing. Reading the first as the second is how a batch that
#: produced no metrics came to look like a batch whose metrics were simply not interesting.
OUTPUT_MANIFEST = "_execution/conversion_outputs.txt"

#: Where the campaign tree lives inside the Job's pod, mounted from one ``emptyDir`` into
#: every container. There is exactly one copy of the data in the pod: the stage container
#: writes it, the conversion reads and writes it in place at campaign-relative paths, and
#: the host container reads it and uploads. Separate input and output trees would mean
#: either a second copy of the run data or a merge step, and the campaign-relative paths
#: are what let outputs go straight to their canonical keys.
CAMPAIGN_MOUNT = "/campaign"

#: Group every container in this pod shares, so one campaign tree can be written by all of
#: them. They do not share a user: the controller image runs as root and an execution image
#: runs as its own unprivileged user (1000 for the family's), and the tree is created by one
#: and written by the other. ``fsGroup`` makes the kubelet group-own the shared volume and
#: set setgid on it, so everything created inside inherits the group -- and the containers
#: that create directories do so with a group-writable umask, because inheriting a group
#: buys nothing if the mode denies it write.
#:
#: 1000 rather than a derived value: it is the family images' own user, and an execution
#: image that runs as something else still shares this group through ``supplementalGroups``.
CAMPAIGN_TREE_GID = 1000

#: Name of the container that stages the campaign's run data into the Job's pod.
STAGE_CONTAINER = "stage"

#: Name of the container that converts what needs the campaign's own execution image. It is
#: the one container in this pod that is not ours, so several rules are stated in terms of
#: it -- no credentials, no umask of ours, writes only the shared mount.
CONVERT_CONTAINER = "convert"

#: Name of the container that runs everything after the conversion.
HOST_CONTAINER = "host"

#: Where postprocessing ranks in the admission queue. Above a campaign's trials (0) and above
#: a calibration probe (1).
#:
#: **Because this is the step that turns a finished campaign into results, and it is one short
#: pod.** A trial that waits is a campaign progressing more slowly; postprocessing that waits
#: is a campaign that has already spent all its compute and has nothing to show for it -- and
#: it is submitted last by construction, so on a cluster kept full by other campaigns a queue
#: ordered by submission alone would never reach it. It also releases its reservation in
#: seconds to minutes rather than for the length of a batch, so ranking it first delays the
#: work below it by very little.
#:
#: Above a probe for the same reason it is above trials, and the two barely compete: a probe
#: gates its own campaign's runs, while this gates a finished campaign's output.
POSTPROCESS_PRIORITY = 2

#: Distinguishes this campaign's postprocessing from its trials in the queue's ledger, which
#: is keyed by owner. Same device as the calibration probe's suffix, and for the same reason:
#: one campaign has two kinds of work outstanding, and a refusal message must name which.
_POSTPROCESS_OWNER_SUFFIX = ":postprocess"

_POLL_SECONDS = 5
_DEFAULT_TIMEOUT = 3 * 60 * 60

#: Disk the pod reserves and may use, for every step. Not settable by a campaign.
#:
#: The request is what keeps a campaign's worth of staged data from landing on a node that
#: reserved no disk for it -- without it the node hits disk pressure and evicts the campaign
#: pods running beside it. It stays split from the limit, unlike cpu and memory: disk is
#: reclaimed as the conversion writes its outputs and the staged bags are dropped, so a
#: ceiling near the reservation would fail a large campaign that never held that much at once,
#: while a reservation near the ceiling would price every postprocessing pod at a disk figure
#: almost none of them reach.
POSTPROCESS_EPHEMERAL_REQUEST = "20Gi"
POSTPROCESS_EPHEMERAL_LIMIT = "200Gi"


def step_resources(cpu, memory) -> dict:
    """One step's ``resources``, with cpu and memory as reservation *and* ceiling.

    **The equality is the point, and it is about comparability rather than thrift.** This pod
    runs on the nodes that run trials, so a step allowed past its reservation takes cores from
    a run whose own request was honest -- and that run's timing becomes a function of which
    campaign happened to be postprocessing beside it. That is precisely the hidden variable
    the CPU governor work exists to remove, reintroduced from a direction nothing downstream
    looks at: no artifact of the affected run records that a conversion was running.

    Nothing here is under test, so the throughput given up is real and the measurement it
    protects is worth more.
    """
    quantities = {"cpu": str(cpu), "memory": str(memory)}
    return {
        "requests": dict(quantities, **{"ephemeral-storage": POSTPROCESS_EPHEMERAL_REQUEST}),
        "limits": dict(quantities, **{"ephemeral-storage": POSTPROCESS_EPHEMERAL_LIMIT}),
    }


#: What the stage step gets. **Fixed, and a campaign's figure does not raise it** -- unlike
#: the host step below.
#:
#: Staging lists the campaign's objects a page at a time and streams one object at a time to
#: disk, so its footprint is set by that construction and not by the size of the campaign. The
#: small memory bound is therefore a GUARD rather than a reservation: a regression in that
#: streaming shows up as this step failing, and a limit that grew with whatever the campaign
#: asked for is exactly the limit that would absorb it silently. This step also runs only our
#: own code, so there is nothing here whose appetite a ``.vast`` would know better than we do.
POSTPROCESS_STAGE_RESOURCES = step_resources(2, "1Gi")

#: The floor under the host step, which is where **everything the campaign declared that is
#: not a rosbag conversion runs** -- its own metric plugins, metadata, publication, the health
#: checks and the index ingest (see :func:`run_host_postprocessing`, which runs the ordinary
#: pipeline with only the rosbag steps skipped).
#:
#: That is why a campaign's figure raises this step too. A knob that sized only the conversion
#: would leave the steps most likely to need memory -- a campaign's own analysis code, whose
#: appetite RoboVAST cannot know -- pinned at a figure they could not change, and the symptom
#: would be an OOM kill of a step whose declared allocation said it had room.
POSTPROCESS_HOST_FLOOR = {"cpu": 2, "memory": "4Gi"}


def raised_to(floor: dict, declared: dict) -> dict:
    """*floor*, or *declared* where that asks for more. Never less than *floor*.

    **Raise-only, and the asymmetry is the point.** A campaign knows when its own analysis
    needs more than the default and should get it. It cannot know that the index ingest still
    fits in less -- and being wrong in that direction is not a slow step but an OOM kill of
    the step that publishes the results, so the floor holds whatever the ``.vast`` says.
    """
    out = dict(floor)
    for key, convert in (("cpu", to_cores), ("memory", to_bytes)):
        want = (declared or {}).get(key)
        if want is None:
            continue
        try:
            if convert(want) > convert(floor[key]):
                out[key] = want
        except (TypeError, ValueError):
            # An unparseable quantity keeps the floor. The config layer refuses these, so
            # reaching here means a caller bypassed it; the floor is the safe answer.
            continue
    return out


def pod_sizing(manifest: dict):
    """What the scheduler will charge this pod, as a :class:`~.node_admission.JobSizing`.

    **The maximum over the steps, not their sum**, which is where a reader will reach for
    ``JobSizing``'s own docstring and be misled: a pod's request *is* the sum of its
    containers' for a pod of ordinary containers, and that is what it says. Staging and
    conversion here are initContainers, which run to completion one at a time before the main
    container starts, so Kubernetes charges ``max(max(init requests), sum(container
    requests))``. Asking for the sum would demand something like ten cores on behalf of a pod
    that requests four, and the queue would hold it out of a cluster that had room -- the
    opposite of what this exists to fix.
    """
    from .node_admission import JobSizing  # noqa: PLC0415

    spec = manifest["spec"]["template"]["spec"]

    def _requests(container, resource, convert):
        raw = ((container.get("resources") or {}).get("requests") or {}).get(resource)
        return (convert(raw) or 0) if raw is not None else 0

    def _charge(resource, convert):
        inits = [_requests(c, resource, convert) for c in spec.get("initContainers", [])]
        mains = sum(_requests(c, resource, convert) for c in spec.get("containers", []))
        return max([*inits, mains]) if (inits or mains) else 0

    return JobSizing(cpu=_charge("cpu", to_cores),
                     memory=int(_charge("memory", to_bytes)))


def _pin_to(manifest: dict, node_id) -> dict:
    """Confine the pod to the operator's node pool, then to the node admission granted.

    The pin is what makes the grant mean something: the queue found room on a particular
    machine, and a pod free to land anywhere can still arrive at a full one -- which is the
    ``Unschedulable`` this path exists to avoid. The pool must reach the pod for the reason
    the trial path gives: the budget provider counts only nodes inside it, so a pod outside
    would run on capacity nothing reserved.
    """
    spec = manifest["spec"]["template"]["spec"]
    selector = {**(spec.get("nodeSelector") or {}), **job_node_pool()}
    if node_id:
        selector[NODE_ID_LABEL] = node_id
    if selector:
        spec["nodeSelector"] = selector
    return manifest


def await_admission(admission, campaign_id: str, name: str, manifest: dict,
                    timeout: float = _DEFAULT_TIMEOUT, poll: float = _POLL_SECONDS) -> tuple:
    """Wait for the queue to find room for this pod. Returns ``(ok, node_id, message)``.

    **Why this pod queues at all.** Its cpu request equals its limit, so on a cluster kept
    full by other campaigns' trials -- which pack by request and burst past it -- no node has
    that much *free*, and a pod created regardless is simply ``Unschedulable``. Leaving it
    Pending for Kubernetes to place later is not the alternative it looks like: this
    deployment reads an unschedulable pod as a failure and says so, which is right for a pod
    that can never fit and wrong for one waiting behind work that will finish. The queue is
    what tells those apart -- :meth:`~.node_admission.AdmissionController.preflight` refuses
    the first permanently, and the second is an ordinary wait.

    The grant is recorded here and the Job created by the caller immediately after, rather
    than from inside the callback: everything the create has to get right -- adopting a Job
    already in flight, replacing a finished one of the same name, owning the ConfigMap it
    mounts -- is a sequence this must not be threaded through. The reservation is held from
    the grant until :func:`run_conversion_job` releases it, so it spans the pod's whole life;
    the window in which the queue believes a pod exists slightly before it does is the width
    of one API call, and the next budget reading reconciles it against the real pod anyway.
    """
    from .node_admission import CREATED, AdmissionRefused  # noqa: PLC0415
    from .node_admission import campaign_start_key  # noqa: PLC0415

    sizing = pod_sizing(manifest)
    granted = {}

    try:
        # Permanent, so it raises rather than waits: a pod larger than any node in the
        # cluster is a figure to change, and waiting for a machine that does not exist would
        # hold the campaign's results forever with nothing said.
        admission.preflight(sizing)
    except AdmissionRefused as exc:
        return False, None, (
            f"postprocessing needs {sizing.cpu:g} cpu / {sizing.memory // 1024 ** 2}Mi and "
            f"no node in this cluster is that large. Lower results_processing.resources for "
            f"this campaign. ({exc})")

    def _record_grant(node_id):
        granted["node_id"] = node_id

    owner = f"{campaign_id}{_POSTPROCESS_OWNER_SUFFIX}"
    admission.submit(owner, [(name, sizing, _record_grant)],
                     started_at=campaign_start_key(campaign_id),
                     priority=POSTPROCESS_PRIORITY)

    deadline = time.monotonic() + timeout
    logged = 0.0
    while time.monotonic() < deadline:
        # Works the GLOBAL queue, like every other caller: whichever thread is awake advances
        # everybody, which is what keeps the queue free of a thread of its own.
        admission.drain()
        if admission.states(owner).get(name) == CREATED:
            return True, granted.get("node_id"), ""
        reason = admission.refusal(owner)
        if reason and time.monotonic() - logged > 60:
            logged = time.monotonic()
            logger.info("Postprocessing of %s is queued for capacity: %s", campaign_id,
                        reason)
        time.sleep(poll)

    admission.finished(name)
    return False, None, (
        f"postprocessing waited {timeout:g}s for {sizing.cpu:g} cpu / "
        f"{sizing.memory // 1024 ** 2}Mi and the cluster stayed full. The campaign's runs "
        f"are published; re-run postprocessing when there is room, or lower "
        f"results_processing.resources.")


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
    # No pre-check that execution.yaml exists. It is the RICHEST record, not the only one:
    # campaign_image_record falls back to launch.yaml, which is written before the first job
    # and therefore survives a campaign whose execution record was never written. Refusing on
    # the file's absence defeated that fallback and stranded finished campaigns -- every run
    # on disk, every bag intact, and no way to convert them -- so absence is left to the "no
    # image recorded anywhere" check below, which is the condition that actually matters.
    record = campaign_image_record(campaign_dir)
    if image_is_pullable(record.campaign_digest):
        return record.campaign_digest
    scenario = record.role(SCENARIO_CONTAINER)
    # `launched` after `declared`, never instead of it: declared is what the campaign asked
    # for, launched is what the launch resolved before the first job existed. They agree on a
    # healthy campaign, and only launched survives one whose execution record was never
    # written -- which is the case this chain exists to rescue.
    image = (record.campaign_image
             or (scenario.declared if scenario else "")
             or next((r.declared for r in record.roles.values() if r.declared), "")
             or (scenario.launched if scenario else "")
             or next((r.launched for r in record.roles.values() if r.launched), ""))
    if not image:
        raise ValueError(
            f"no execution image recorded for this campaign (looked in {path} and in "
            "_execution/launch.yaml); cannot pick the image whose custom ROS2 types the "
            "rosbags need")
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

    ``_execution/`` is fetched unconditionally and first, because it is the one part that
    must arrive whatever else did: the campaign's POSTPROCESSING section, the outcome and
    the provenance live there, and it is a fixed prefix holding a handful of small files
    rather than something that has to be discovered. A failure in the pod produces exactly
    that and no outputs, which is the case where the manifest is the thing that is missing.
    """
    import os  # noqa: PLC0415

    from . import in_pod_storage  # noqa: PLC0415

    bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
        cluster_config, campaign_id)
    storage = in_pod_storage.storage_client_for(cluster_config)

    execution = storage.download_prefix(
        bucket, f"{campaign_prefix}_execution",
        os.path.join(str(campaign_root), "_execution"), force=force)

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
    return n + execution


def _write_failure_log(cluster_config, campaign_id: str,  # pylint: disable=unused-argument
                       campaign_root, log_path: str, message: str) -> None:
    """Write the POSTPROCESSING phase file when the Job produced none.

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

    # The message still carries POINTER_SLOT: the pointer is decided after this file is
    # written, precisely BY whether it was. Inside the file the slot has nothing to say --
    # the reader is already in the section it would point at.
    headline = message.replace(POINTER_SLOT, "").strip()
    lines = [
        f"Postprocessing failed: {headline}",
        "",
        "No postprocessing log arrived, so the Job failed in an initContainer: they run "
        "to completion before the host container starts, so a failure there means the "
        "step that writes this log never ran. The Job first stages the campaign's "
        "recorded run data into the pod and then, where the campaign needs it, converts "
        "its rosbags; those are the two candidates.",
        "",
        "Which of them, and why, is in the line above: each stage exits with a code of "
        "its own and the pod's status carries it whatever happened to the container. "
        "That is the one channel that always survives -- a stage that failed because the "
        "object store was unreachable cannot upload an explanation, and a container the "
        "kubelet killed under node disk pressure runs no cleanup at all. Node disk and "
        "the object store are what to check.",
    ]
    text = "\n".join(lines) + "\n"
    logger.warning("Postprocessing failed before its host step ran; recording the account "
                   "for %s", campaign_id)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        logger.warning("Could not write the postprocessing log for %s: %s", campaign_id, e)


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


def _read_submit_inputs(read_root: str, skip=None, skip_rosout: bool = False) -> tuple:
    """``(rosbag_cmds, image, tolerate_under, convert_resources)`` from a campaign tree.

    The four facts the manifest needs about a campaign, and all four come from files: the
    ``.vast`` says whether a conversion is configured at all and how much it may use,
    ``execution.yaml`` names the image its rosbags deserialize in, and the intervention
    ledger names the runs whose bags were cut short mid-write.
    """
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        postprocess_convert_resources)
    from robovast.results_processing.postprocessing_plugins import (  # noqa: PLC0415
        _interrupted_job_dirs)

    vast_path = campaign_vast(read_root)
    rosbag_cmds = rosbag_commands_for(vast_path, skip=skip, skip_rosout=skip_rosout)
    if not rosbag_cmds:
        # No image is resolved at all for a host-only campaign: nothing in the pod pulls
        # one, so a campaign whose execution image has since gone from the registry still
        # postprocesses. The sizing goes the same way: with no conversion container there is
        # nothing for it to size.
        return [], None, (), None
    # The same seam the local lane reads, for the same reason: a bag belonging to a job
    # that was stopped by hand or invalidated by the runner cannot be opened, ever, and
    # must not fail the conversion for every job that finished.
    return (rosbag_cmds, campaign_execution_image(read_root),
            tuple(_interrupted_job_dirs(read_root)),
            postprocess_convert_resources(vast_path))


def _submit_inputs(cluster_config, campaign_id: str, campaign_root: str,
                   skip=None, skip_rosout: bool = False) -> tuple:
    """:func:`_read_submit_inputs`, against a campaign the submitter may not hold.

    The campaign lives in the object store and the pod is what stages it, so the submitting
    process cannot assume a populated root -- but the manifest depends on three facts about
    the campaign, so it cannot be built without reading them either. All three are single
    small files, so this assembles exactly those rather than a campaign in order to answer
    three questions about it.

    **Assembled per file, never chosen wholesale.** A local copy is preferred where there is
    one -- the controller built the root, or a raw archive was imported into it, and fetching
    over that could only replace a file with the store's copy of itself. But "is this root
    local?" has no single answer: the service's cache dir holds whatever earlier calls put
    there, so a root can carry the ``.vast`` and not ``execution.yaml``. Deciding from one
    file that the rest are present is how that partial state turns into "no such file" on
    the next read, at submit time, on a campaign whose results are fine.

    ``_config/`` needs a listing because the ``.vast``'s name is the campaign's own; the
    other two are at fixed paths.
    """
    import glob  # noqa: PLC0415
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from . import in_pod_storage  # noqa: PLC0415

    root = str(campaign_root)
    local_vast = sorted(glob.glob(os.path.join(root, "_config", "*.vast")))
    wanted = ["_execution/execution.yaml", "_execution/interventions.json"]
    if local_vast and all(os.path.isfile(os.path.join(root, *w.split("/")))
                          for w in wanted):
        # EVERY file the read needs, the intervention ledger included. Absence of that file
        # is not absence of interventions: its reader answers "nobody intervened" either
        # way, so a root that happens not to hold it silently drops every bag the
        # conversion was supposed to tolerate -- and the bags in that ledger are the ones
        # that cannot be opened at all, so dropping them fails the whole conversion.
        return _read_submit_inputs(root, skip=skip, skip_rosout=skip_rosout)

    bucket, prefix = in_pod_storage.campaign_storage_location(cluster_config, campaign_id)
    storage = in_pod_storage.storage_client_for(cluster_config)
    with tempfile.TemporaryDirectory(prefix="robovast-postproc-") as tmp:
        if local_vast:
            rel = os.path.join("_config", os.path.basename(local_vast[0]))
            os.makedirs(os.path.join(tmp, "_config"), exist_ok=True)
            shutil.copyfile(local_vast[0], os.path.join(tmp, rel))
        else:
            # Sorted and first: a campaign has one .vast, and a deterministic choice keeps
            # two submissions of the same campaign from disagreeing if it ever has two.
            keys = sorted(k for k in storage.list_keys(bucket, f"{prefix}_config/")
                          if k.endswith(".vast"))
            if not keys:
                raise ValueError(
                    f"campaign {campaign_id} has no .vast under its _config/, locally or in "
                    "the object store; postprocessing cannot tell what it configures")
            wanted.insert(0, keys[0][len(prefix):])
        for rel in wanted:
            dst = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            src = os.path.join(root, *rel.split("/"))
            if os.path.isfile(src):
                shutil.copyfile(src, dst)
            else:
                # Unchecked: the readers below name a genuinely missing input better than a
                # check here could, and the ledger is allowed to be absent.
                storage.download_object(bucket, f"{prefix}{rel}", dst)
        return _read_submit_inputs(tmp, skip=skip, skip_rosout=skip_rosout)


def postprocess_campaign(cluster_config, campaign_id: str,  # pylint: disable=unused-argument
                         campaign_root: str, namespace: str, force: bool = False,
                         skip=None, skip_rosout: bool = False,
                         kube_context=None, state=None, admission=None) -> tuple:
    """Analysis postprocessing for one campaign, in-cluster. Returns ``(ok, message)``.

    ``ok`` carries :func:`run_conversion_job`'s three values through unchanged, ``None``
    among them: this process losing sight of the Job is not the Job failing, and the
    persisting callers key off ``None`` to leave the campaign's recorded outcome alone.

    The single implementation behind both entry points — the per-campaign controller
    (auto-chain) and the service (explicit re-run). All of the work happens in the Job:

    1. **submit** the Job and wait for it — it stages the campaign into its pod,
       converts rosbags where the campaign asks for it, and runs the host steps (index
       ingest, metadata) in its last container;
    2. **sync** its outputs into *campaign_root* — done **regardless of the Job's
       outcome**, so a failed run's ``postprocessing.log`` lands in the campaign log the
       web UI shows, and so this process's status objects describe what actually exists.

    *campaign_root* is where the Job's outputs are pulled down to, and does **not** have
    to hold the campaign: the pod stages the campaign itself, and the three facts the
    manifest needs about it are read by :func:`_submit_inputs`, from the store when they
    are not local. A root that does hold it — the controller built it there, or a raw
    archive was imported into it — is used as it stands.

    *kube_context* must be the same context the campaign's Jobs were submitted with;
    ``None`` means the active kubeconfig context, which is only correct when the caller
    has none of its own.

    *state* is accepted for the caller's call shape and stays empty on this lane: every
    step now runs in a pod, so a step's line reaches this process only once the Job ends
    and its log is synced, and a live ``stage`` marker fed from here would be a marker
    that only ever moves after the phase it describes is over.
    """
    rosbag_cmds, image, tolerate_under, convert_resources = _submit_inputs(
        cluster_config, campaign_id, campaign_root, skip=skip, skip_rosout=skip_rosout)
    if not rosbag_cmds:
        logger.info("Campaign %s configures no rosbag conversion; the Job runs its host "
                    "steps only, and stages the campaign without its rosbags", campaign_id)
    ok, message = run_conversion_job(
        cluster_config, campaign_id, namespace, image, rosbag_cmds, force=force,
        kube_context=kube_context, tolerate_under=tolerate_under, skip=skip,
        convert_resources=convert_resources, admission=admission)
    return record_job_outputs(cluster_config, campaign_id, campaign_root, ok, message,
                              force=force)


def record_job_outputs(cluster_config, campaign_id: str, campaign_root: str,
                       ok: bool, message: str, force: bool = False) -> tuple:
    """Pull down what the Job produced and turn its verdict into ``(ok, message)``.

    Everything a postprocessing Job's outcome means for the campaign tree, in one place, so
    a process that submitted the Job and one that only waited for it leave the campaign in
    the same state. Split from :func:`postprocess_campaign` for that second caller: a
    re-attach has no submit half and must not grow a second account of a failure.
    """
    import os  # noqa: PLC0415

    # Sync the Job's outputs regardless of outcome. The pod tees its stdout/stderr to
    # postprocessing.log and uploads what it produced even on failure, so this lands the
    # POSTPROCESSING section (with the error) in the campaign log the web UI shows and
    # finalize uploads — without it, a failure surfaces only as a terse "kubectl logs"
    # hint the user cannot act on off-cluster. It is also what makes the campaign root
    # this process serves its status objects from match what the Job wrote.
    # force rides along: it made the Job bypass its caches and REPLACE the CSVs, and
    # the fetch skips same-size files unless told not to.
    sync_outputs(cluster_config, campaign_id, campaign_root, force=force)
    log_path = os.path.join(str(campaign_root), "_execution", "postprocessing.log")
    publish_postprocessing_log(cluster_config, campaign_id, campaign_root)
    if ok:
        return True, message
    if ok is None:
        # Passed through untouched, and in particular no failure log is authored: that log
        # is the account of a fault, and there is no fault to account for -- the Job may be
        # converting still. The synced log above is whatever the Job has written so far,
        # which is exactly what a reader wants while the outcome is open.
        logger.warning("Postprocessing outcome unknown: %s", message)
        return None, message
    # Echo the error to the service console too. The web UI already has it via the
    # synced postprocessing.log (POSTPROCESSING section); no campaign log handler is
    # attached at this point, so this reaches the ``vast serve`` stdout only — not
    # duplicated into the campaign log.
    #
    # The sync above is also what settles WHERE the message may send the reader: a Job
    # that died in an initContainer uploaded no log, so the section it would name does
    # not exist. Deciding here is the whole point — this is the first place that can tell
    # the two apart.
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8") as f:
            logger.warning("Postprocessing failed:\n%s", f.read().rstrip())
    else:
        _write_failure_log(cluster_config, campaign_id, campaign_root, log_path, message)
    # Written and published together: this is the whole account of a failure whose Job is
    # reaped 300 s later, so the window in which it can still be published is the one it
    # was written in.
    publish_postprocessing_log(cluster_config, campaign_id, campaign_root)
    return False, with_log_pointer(message, log_path)


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

    This is the CAMPAIGN-level pass, and it reads ``results_processing.postprocessing``.
    A search's per-batch pass is a different list -- ``search.postprocessing`` -- and runs
    through :func:`~robovast.results_processing.postprocessing.run_postprocessing_commands`
    instead, which is the function both lists share.
    """
    from robovast.execution.control_server import stage_output_callback  # noqa: PLC0415
    from robovast.results_processing.postprocessing import ROSBAG_JOB_NAMES  # noqa: PLC0415
    from robovast.results_processing.postprocessing import run_postprocessing

    return run_postprocessing(
        results_dir=results_dir, campaign=campaign_id, force=force,
        skip=sorted(set(skip or ()) | set(ROSBAG_JOB_NAMES)),
        output_callback=stage_output_callback(state, logger.info))


#: Campaign-relative path of the conversion's log. This Job pod runs in a separate context
#: from the controller, so its stdout is otherwise only a transient ``kubectl logs``. Teeing
#: the conversion output here puts it in the campaign tree the host container uploads, where
#: :func:`sync_outputs` lands it at ``<campaign_root>/_execution/postprocessing.log`` — the
#: POSTPROCESSING section of the unified campaign log. The host container appends to the
#: same file, so the two read as one ordered section.
_POSTPROC_LOG_REL = "_execution/postprocessing.log"

#: Campaign-relative path where the conversion records what it produced from what.
#:
#: The conversion must pass ``--provenance-file``: the host steps run with the ``rosbags_*``
#: steps *skipped*, so they have nothing to record for them, and a campaign whose conversion
#: passed none carries a ``postprocessing_steps`` table naming only the host's own steps
#: while the conversion's had run. The local lane passes one, so without this the provenance
#: a campaign carries would depend on the lane it ran on.
_ROSBAG_PROVENANCE_REL = "_execution/rosbags_provenance.json"


def _campaign_dir(campaign_id: str) -> str:
    """Where the campaign tree sits inside the pod: the stage container's destination.

    One definition, because three containers have to agree on it: the stage container is
    told it as ``ROBOVAST_STAGE_DEST`` plus the campaign id, the conversion's arguments are
    built from it here, and the host container resolves the same path from the same two
    environment values.
    """
    return f"{CAMPAIGN_MOUNT}/{campaign_id}" if campaign_id else CAMPAIGN_MOUNT


def _conversion_script(rosbag_cmds: list, force: bool, tolerate_under=(),
                       campaign_id: str = "") -> str:
    """The conversion initContainer's shell: convert each batch, in place.

    Reads and writes the shared campaign mount and nothing else. **No object-store
    credentials and no upload:** this container runs an arbitrary user image (the system
    under test's), and the host container that follows it is what talks to the store, so
    there is nothing here for a credential to be needed for.

    ``--output-root`` is the campaign tree itself, so every output lands at its
    campaign-relative path and the host container can upload it to its canonical key
    without a mapping step.

    All setup and conversion stdout/stderr is teed into the campaign's
    ``postprocessing.log`` so it becomes the POSTPROCESSING section of the unified campaign
    log; the host container appends to the same file. ``pipefail`` preserves the
    conversion's exit status through the ``tee`` pipe.
    """
    root = _campaign_dir(campaign_id)
    log = f"{root}/{_POSTPROC_LOG_REL}"
    convert = []
    for params in rosbag_cmds:
        args = [
            "/scripts/ros2_exec.sh", "/scripts/rosbags_process.py",
            "--config", _shquote(json.dumps({"plugins": params.get("plugins", [])})),
            "--output-root", _shquote(root),
            "--provenance-file", _shquote(f"{root}/{_ROSBAG_PROVENANCE_REL}"),
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
        # shared seam for this rule and which both lanes consult.
        for job_dir in tolerate_under:
            args += ["--tolerate-under", _shquote(str(job_dir))]
        args.append(_shquote(root))
        convert.append(" ".join(args))

    lines = [
        "set -eo pipefail",
        # The log directory is created before anything else, and the setup runs inside the
        # tee, because setup under `set -e` is exactly where the silent failures live: an
        # unwritable campaign tree aborts before the conversion's own output, and the
        # campaign is then pointed at a POSTPROCESSING section that does not exist.
        f"mkdir -p $(dirname {log}) || exit 1",
        "rc=0",
        "(",
        "  set -e",
        "\n".join("  " + c for c in convert),
        f') 2>&1 | tee -a "{log}" || rc=$?',
        # AFTER the tee'd block and outside it, on purpose. What the conversion used is
        # worth recording whether or not it succeeded -- a conversion killed for exceeding
        # its memory is exactly the case the record exists for -- and it must not be able to
        # change `rc`, which is the conversion's own verdict.
        postprocess_usage.shell_record(root, CONVERT_CONTAINER),
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

    # The resource sampler travels with them, from the package that owns it rather than a
    # copy: the conversion container is the campaign's own image, so it can import nothing
    # of robovast, and this is the only way it can report what it cost. Its container-level
    # probes are stdlib-only -- psutil is imported inside the per-process loop and that loop
    # is not what `--once` runs -- so it works in an image that has no psutil.
    sampler = files("robovast.execution.data") / "monitor_resources.py"
    payload["monitor_resources.py"] = sampler.read_text(encoding="utf-8")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _scripts_cm_name(campaign_id, discriminator),
            "namespace": namespace,
            "labels": {"jobgroup": POSTPROCESS_JOBGROUP,
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
#: setup ahead of its first ``tee``; or -- the one that leaves no trace at all -- the stage
#: initContainer can fail while copying the campaign's run data into the pod, in which case
#: nothing after it starts and there is nothing anywhere to tee. Staging is the likelier of
#: the two on a campaign of any size: it pulls the campaign onto the node, so it is the step
#: that meets a full disk first. Which one it was is in the pod's exit status, which
#: :func:`pod_failure_reason` reads and puts ahead of this.
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


#: :func:`_adopt_or_replace` kept a live Job and the caller is now a waiter on someone
#: else's Job. It decides ownership, not just control flow: everything that Job mounts --
#: the scripts ConfigMap above all -- belongs to the attempt that created it, and a waiter
#: must write none of it and delete none of it.
_JOB_ADOPTED = "adopted"

#: The Job answering to this name was finished and has been deleted and re-created, so the
#: caller owns this one and the resources it mounts.
_JOB_RECREATED = "recreated"


def _live_job(batch, namespace: str, name: str) -> bool:
    """Is a Job of this name present AND still active?

    Separate from :func:`_adopt_or_replace` because the answer is needed *before* anything
    is written: a live Job's mounted resources are not ours to touch, and that has to be
    known before the first write rather than discovered from a 409 after it.

    A Job that cannot be read -- absent, or an API error -- is not live. Nothing is adopted
    on a maybe: the create that follows is the authority on whether the name is free.
    """
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    try:
        existing = batch.read_namespaced_job(name=name, namespace=namespace)
    except ApiException:
        return False
    return bool(getattr(getattr(existing, "status", None), "active", None))


def _adopt_or_replace(batch, namespace: str, name: str, manifest: dict) -> str:
    """Keep a still-running Job; delete and re-create a finished one.

    Returns :data:`_JOB_ADOPTED`, :data:`_JOB_RECREATED`, or ``""`` if neither held. The
    caller needs the two apart rather than a bare success: only the re-created case owns
    the Job's mounted resources -- see :data:`_JOB_ADOPTED`.

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
                return _JOB_RECREATED
            except ApiException:
                return ""
        return ""

    status = getattr(existing, "status", None)
    if getattr(status, "active", None):
        logger.info("Postprocessing job %s is already running; waiting on it", name)
        return _JOB_ADOPTED

    logger.info("Replacing finished postprocessing job %s", name)
    try:
        batch.delete_namespaced_job(name=name, namespace=namespace,
                                    propagation_policy="Foreground")
    except ApiException as e:
        if e.status != 404:
            logger.warning("Could not delete %s: %s", name, e)
            return ""

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            batch.read_namespaced_job(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                break
            return ""
        time.sleep(_POLL_SECONDS)
    else:
        logger.warning("Postprocessing job %s did not go away", name)
        return ""

    try:
        batch.create_namespaced_job(namespace=namespace, body=manifest)
    except ApiException as e:
        logger.warning("Could not re-create %s: %s", name, e)
        return ""
    return _JOB_RECREATED


#: How often the Job's own log is published to the campaign's phase file while it runs.
#:
#: The pod is the writer here, and nothing it writes leaves the pod until it exits: its log
#: lives on a shared volume and is uploaded by the last container at the end. So a
#: postprocess that takes twenty minutes showed an empty POSTPROCESSING section for twenty
#: minutes, and the only way to watch it was ``kubectl logs`` against a pod name nobody
#: off-cluster has.
#:
#: An object store has no append, so each publish re-uploads the whole log -- which is why
#: this is not every poll. Thirty seconds is slow enough for that to be a rounding error
#: against a conversion measured in minutes, and fast enough to read as progress.
_LIVE_LOG_INTERVAL = 30.0

#: Sort key for a pod whose creation time the API did not fill in, so ordering by it never
#: raises. Such a pod loses to any pod that has one, which is the right way round: a
#: timestamp is present on anything the scheduler has seen.
_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def publish_live_log(core, cluster_config, campaign_id: str, namespace: str,
                     job_name: str) -> bool:
    """Publish the running Job's log as the campaign's POSTPROCESSING phase. False if not.

    Read from the pod rather than from the volume it writes: the volume is the pod's own and
    nothing outside can see it, while the log is on the pod's stdout by construction -- the
    conversion tees it there and the host step logs to it.

    Every container's output in declaration order, so staging and conversion read as one
    section in the order they ran. A container that has not started yet has no log and is
    skipped, which is also how "this stage has not run" should look.

    Best-effort throughout: this is a read for someone watching, and it must not fail the
    postprocess it is watching.
    """
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from kubernetes import client  # noqa: PLC0415

    from . import in_pod_storage  # noqa: PLC0415
    try:
        pods = core.list_namespaced_pod(namespace=namespace,
                                        label_selector=f"job-name={job_name}").items or []
        if not pods:
            return False
        # The NEWEST pod, not whichever the listing put first. A Job can have more than one
        # -- a backoffLimit retry makes another, and replacing a finished Job of the same
        # name makes another still -- and the listing does not promise an order. Publishing
        # from an arbitrary one would make the section alternate between two attempts as
        # this is called again, which reads worse than either of them.
        pod = max(pods, key=lambda p: (getattr(p.metadata, "creation_timestamp", None)
                                       or _EPOCH, p.metadata.name))
        names = [c.name for c in (pod.spec.init_containers or [])]
        names += [c.name for c in (pod.spec.containers or [])]
        chunks = []
        for container in names:
            try:
                text = core.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=namespace, container=container)
            except client.exceptions.ApiException:
                continue          # not started, or already gone: no output to place
            if text:
                chunks.append(text if text.endswith("\n") else text + "\n")
        if not chunks:
            return False
        bucket, prefix = in_pod_storage.campaign_storage_location(cluster_config,
                                                                  campaign_id)
        storage = in_pod_storage.storage_client_for(cluster_config)
        # Through a file because the client uploads paths, not bytes. Named for the campaign
        # so two of these running at once cannot write each other's log.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".log",
                                         prefix=f"live-{campaign_id}-",
                                         delete=False) as handle:
            handle.write("".join(chunks))
            staged = handle.name
        try:
            storage.upload_file(staged, bucket, f"{prefix}{_POSTPROC_LOG_REL}")
        finally:
            os.unlink(staged)
        return True
    except Exception as e:  # noqa: BLE001 - a read for a watcher may not fail the work
        logger.debug("could not publish the live postprocessing log of %s: %s",
                     campaign_id, e)
        return False


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
        # Init containers first and in declaration order: staging runs before everything
        # else, so when staging is what failed the later containers' statuses say nothing.
        statuses = list(getattr(status, "init_container_statuses", None) or []) + \
            list(getattr(status, "container_statuses", None) or [])
        for cs in statuses:
            state = getattr(cs, "state", None)
            term = getattr(state, "terminated", None) if state else None
            code = getattr(term, "exit_code", None) if term else None
            if isinstance(code, int) and code != 0:
                name = getattr(cs, "name", None) or "?"
                # The stage container's own vocabulary, where it has one. `exited 1
                # (Error)` is every failure at once and names none of them; `exited 42` is
                # a code chosen in order to be read.
                from .postprocess_stage import STAGE_EXIT_REASONS  # noqa: PLC0415
                if name == STAGE_CONTAINER and code in STAGE_EXIT_REASONS:
                    return f"container {name} {STAGE_EXIT_REASONS[code]}"
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


def _pod_env(names) -> list:
    """Project the named variables out of this pod's own environment.

    The stage and host containers rebuild the cluster config from the environment, so they
    need the same values the service was configured with. Reading them here rather than
    re-deriving them is what keeps the Job pointed at the same store the campaign was
    written to: a second derivation would be a second answer to a question the service has
    already answered.

    A variable that is not set is left out rather than passed empty. The entry points fail
    loudly on a missing one, which is the report that names it; an empty string would be a
    config value that looks present and is not.
    """
    import os  # noqa: PLC0415
    return [{"name": n, "value": os.environ[n]} for n in names
            if os.environ.get(n, "").strip()]


#: The environment the stage and host containers rebuild the cluster config from -- the
#: same contract ``ClusterService`` builds its own from. Named here because the manifest and
#: the entry points must not drift: a value the pod is not given is a container that exits
#: at start.
_CLUSTER_CONFIG_ENV = ("ROBOVAST_CLUSTER_CONFIG_NAME", "ROBOVAST_CLUSTER_CONFIG_KWARGS")


def _index_env(namespace: str) -> list:
    """The index DSN for the host container, with the password kept out of the Job spec.

    A Job spec is printed by ``vast service manifests``, read back with ``kubectl get`` and
    quoted in issues, so a password inline in it is a password in all three. Where the DSN
    names the in-cluster index there is a Secret already holding that password, so this
    passes the DSN without it and the password itself as ``PGPASSWORD`` from that Secret --
    libpq reads that variable whenever the connection string omits a password, so the
    container connects with exactly the credentials it would have had.

    A deployment pointed at an index outside the cluster has no such Secret, and there the
    DSN is passed as it stands: the alternative is a host container that stages a campaign
    and then cannot authenticate, which is worse than a credential in a spec the operator
    configured themselves.
    """
    import os  # noqa: PLC0415

    from robovast.common.index_db import DSN_ENV  # noqa: PLC0415

    from . import index_deploy  # noqa: PLC0415

    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        # Refused here rather than in the pod: the host container's whole purpose is the
        # index ingest, so a Job submitted without a DSN would stage a campaign's worth of
        # data onto a node before failing on config the submitter could already see.
        raise ValueError(
            f"the central index is not configured in this process ({DSN_ENV}); "
            "postprocessing cannot be submitted, because its host step is the index "
            "ingest")
    without_password = re.sub(r"\s*password\s*=\s*\S+", "", dsn).strip()
    if without_password != index_deploy.index_dsn(namespace=namespace):
        return [{"name": DSN_ENV, "value": dsn}]
    return [
        {"name": DSN_ENV, "value": without_password},
        {"name": "PGPASSWORD",
         "valueFrom": {"secretKeyRef": {"name": index_deploy.INDEX_SECRET_NAME,
                                        "key": index_deploy.INDEX_PASSWORD_KEY}}},
    ]


def build_manifest(campaign_id: str, image, rosbag_cmds: list, s3: tuple,
                   namespace: str, force: bool = False,
                   pull_secret_name: str = "", discriminator: str = "",
                   tolerate_under=(), skip=None, batch_commands=None,
                   convert_resources=None) -> dict:
    """Build the postprocessing Job manifest.

    Args:
        campaign_id: The campaign to postprocess.
        image: **The campaign's execution image** (the SUT image from
            ``_execution/execution.yaml``) — required for its custom ROS2 types, and
            required *only* for them. Ignored when *rosbag_cmds* is empty: a campaign with
            no rosbag conversion needs no conversion container, so its execution image is
            never pulled and an image that has since gone from the registry does not stop
            it being postprocessed.
        rosbag_cmds: :func:`rosbag_commands_for` output. Empty means no conversion
            container at all.
        s3: ``(endpoint, access_key, secret_key, bucket, campaign_prefix)``.
        namespace: Kubernetes namespace.
        force: Bypass the per-rosbag caches, and replace what the host step already wrote.
        tolerate_under: Campaign-relative artifact dirs of jobs that were cut short
            (:func:`~robovast.results_processing.postprocessing_plugins._interrupted_job_dirs`).
            Their bags are unreadable by construction, so the conversion reports them and
            succeeds instead of failing the campaign.
        skip: Postprocessing steps the host step must not run.
        convert_resources: ``{"cpu": …, "memory": …}`` the conversion step runs at, from
            :func:`~robovast.results_processing.postprocessing.postprocess_convert_resources`
            -- the campaign's ``results_processing.resources`` over that function's defaults.
            ``None`` takes those defaults, which is what a caller with no ``.vast`` in reach
            must do.
        batch_commands: The ``search.postprocessing`` commands the host must run, for a
            per-batch Job. ``None`` is the campaign-level Job, whose host runs the whole
            ``results_processing.postprocessing`` pass and completes the campaign — index
            ingest, metadata, provenance record. Given, the host runs exactly these and
            completes nothing, because a search reaches this once per batch on a campaign
            that is still growing. The two lists are DIFFERENT blocks of the ``.vast``, so
            a batch Job left to find its own would run the campaign-level one; passing them
            is what keeps the batch running what the controller resolved for it.
            The pod's shape does not change either way; only what the host is asked for does.
        pull_secret_name: Secret for this pod's OWN image pulls -- the controller image
            both robovast containers run, and the campaign's execution image. Without it a
            private-registry deployment sits in ``ImagePullBackOff`` while the Job stays
            ``active``, so the wait reports a timeout naming neither the image nor the
            registry.

    The ``/scripts`` come from a per-campaign ConfigMap (see
    :func:`scripts_configmap_manifest`) built from the driver's own
    ``results_processing/data`` — the K8s analog of ``docker_exec.sh``'s
    ``-v <scripts>:/scripts`` — so the script version always matches the driver. It is
    mounted, and the ConfigMap volume declared, only where a conversion container exists.
    """
    from .cluster_execution import _label_safe_campaign  # noqa: PLC0415
    from .postprocess_host import (ENV_COMMANDS, ENV_FORCE,  # noqa: PLC0415
                                   ENV_SKIP)
    from .postprocess_stage import (ENV_BATCH_JOBS,  # noqa: PLC0415
                                    ENV_CAMPAIGN_ID, ENV_SKIP_BAGS,
                                    ENV_STAGE_DEST)

    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        POSTPROCESS_CONVERT_DEFAULTS)

    sized = convert_resources or POSTPROCESS_CONVERT_DEFAULTS
    convert_resources_block = step_resources(sized["cpu"], sized["memory"])
    host_block = step_resources(**raised_to(POSTPROCESS_HOST_FLOOR, sized))

    endpoint, access_key, secret_key, bucket, campaign_prefix = s3
    safe = _label_safe_campaign(campaign_id)
    s3_env = [
        {"name": "S3_ENDPOINT", "value": endpoint},
        {"name": "S3_BUCKET", "value": bucket},
        {"name": "S3_ACCESS_KEY", "value": access_key},
        {"name": "S3_SECRET_KEY", "value": secret_key},
        {"name": "S3_CAMPAIGN_PREFIX", "value": campaign_prefix},
    ]
    # Every container in this pod, because the campaign log is read from the pod's stdout
    # (see publish_live_log) and stdout here is a pipe rather than a terminal. Python
    # block-buffers a pipe, so without this a step's output reaches the log in ~8 KB clumps
    # long after it happened -- and the reason to publish a running postprocess at all is
    # that someone is watching it. Logging handlers flush per record and are unaffected;
    # what this recovers is `print` and the conversion's own progress output.
    unbuffered_env = [{"name": "PYTHONUNBUFFERED", "value": "1"}]
    # The two robovast containers talk to the store and the index; the conversion does
    # neither, and gets none of this. See the conversion container below.
    robovast_env = s3_env + _pod_env(_CLUSTER_CONFIG_ENV) + [
        {"name": ENV_CAMPAIGN_ID, "value": campaign_id},
        {"name": ENV_STAGE_DEST, "value": CAMPAIGN_MOUNT},
    ]
    campaign_mount = {"name": "campaign", "mountPath": CAMPAIGN_MOUNT}

    stage = {
        "name": STAGE_CONTAINER,
        "image": resolve_controller_image(),
        # `umask 0002` is not cosmetic: this container creates the campaign tree, and the
        # conversion container writes its outputs INTO it as a different user. Root's default
        # umask makes those directories group-readable and not group-writable, so the
        # conversion fails on its first output file with EACCES -- after staging the whole
        # campaign. Inheriting the group via fsGroup buys nothing if the mode denies write.
        "command": ["sh", "-c",
                    "umask 0002 && exec python3 -m "
                    "robovast.execution.cluster_execution.postprocess_stage"],
        # Bags are staged only where something in this pod opens one. The host step never
        # does -- it reads the derived tables and the run metadata -- so a campaign with no
        # conversion container stages the campaign tree WITHOUT its rosbags, which is the
        # bulk of a campaign by orders of magnitude. Staging them anyway would spend the
        # whole download and the whole node disk on data nothing in the pod reads.
        "env": (unbuffered_env + robovast_env
                + ([] if rosbag_cmds else [{"name": ENV_SKIP_BAGS, "value": "1"}])
                # One batch's job artifacts, for a per-batch Job. The bags are the bulk of
                # a campaign and every batch's sit under the same prefix, so without this a
                # search stages every earlier batch again on every batch.
                + ([{"name": ENV_BATCH_JOBS, "value": discriminator}]
                   if batch_commands is not None and discriminator else [])),
        "volumeMounts": [campaign_mount],
        "resources": copy.deepcopy(POSTPROCESS_STAGE_RESOURCES),
    }
    convert = {
        "name": CONVERT_CONTAINER,
        # The system-under-test's own image: custom ROS2 types only deserialize here.
        # ros2_exec.sh sources /opt/ros + /ws/install.
        "image": image,
        "command": ["/bin/bash", "-c",
                    _conversion_script(rosbag_cmds, force, tolerate_under,
                                       campaign_id=campaign_id)],
        # **No store credentials, deliberately.** This container reads and writes the
        # shared campaign mount and nothing else, and it is an arbitrary user image -- the
        # campaign's own -- so it is the one container in this pod that must hold nothing
        # that would let it reach the store or the index. Buffering is not a credential:
        # this is where the conversion's progress output comes from, and it is the longest
        # step, so it is the one whose output most needs to arrive while it runs.
        "env": unbuffered_env,
        "volumeMounts": [
            {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
            campaign_mount,
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": convert_resources_block,
    }
    host = {
        "name": HOST_CONTAINER,
        "image": resolve_controller_image(),
        # Same umask as the stage container, and for a reason that outlives this pod: what
        # this step derives is uploaded as the campaign's own, so a re-run staging it again
        # must be able to write over it.
        "command": ["sh", "-c",
                    "umask 0002 && exec python3 -m "
                    "robovast.execution.cluster_execution.postprocess_host"],
        # The index DSN is injected HERE and nowhere else: this is the only container that
        # writes to the index.
        "env": unbuffered_env + robovast_env + _index_env(namespace) + [
            {"name": ENV_FORCE, "value": "1" if force else "0"},
            {"name": ENV_SKIP, "value": ",".join(sorted(set(skip or ())))},
            *([{"name": ENV_COMMANDS, "value": json.dumps(batch_commands)}]
              if batch_commands is not None else []),
        ],
        "volumeMounts": [campaign_mount],
        "resources": host_block,
    }

    init_containers = [stage]
    if rosbag_cmds:
        init_containers.append(convert)
    # initContainers run sequentially to completion in declaration order and the main
    # containers start only after they all succeed. That ordering IS the orchestration
    # here; there is no code sequencing these steps.
    #
    # The host container runs in BOTH shapes, and the shape decides only what it is asked
    # for. It is the only container given the store, deliberately -- the conversion runs
    # the campaign's own image -- so a Job that ended at the conversion could not send its
    # outputs anywhere, and they would go with the pod's emptyDir.
    containers = [host]

    volumes = [
        # One copy of the campaign, shared by every container.
        {"name": "campaign", "emptyDir": {}},
    ]
    if rosbag_cmds:
        # Scratch for the conversion, which is the only container that mounts it. Declared
        # with that container rather than always: a volume nothing mounts is a volume a
        # reader of this spec has to work out the purpose of.
        volumes.append({"name": "tmp", "emptyDir": {}})
        # The driver's own conversion scripts, executable (0755). Declared only where a
        # container mounts them: a ConfigMap volume whose source does not exist holds the
        # pod in ContainerCreating, and a host-only Job creates no such ConfigMap.
        volumes.insert(0, {"name": "scripts",
                           "configMap": {"name": _scripts_cm_name(campaign_id,
                                                                  discriminator),
                                         "defaultMode": 0o755}})
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": _short_job_name("robovast-postproc-", campaign_id, discriminator),
            "namespace": namespace,
            "labels": {
                "jobgroup": POSTPROCESS_JOBGROUP,
                "campaign-id": safe,
            },
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {
                    "labels": {"jobgroup": POSTPROCESS_JOBGROUP, "campaign-id": safe},
                },
                "spec": {
                    "restartPolicy": "Never",
                    # Campaign nodes are where the bags already are and where this is
                    # allowed to run; without the toleration a deployment that dedicates
                    # its nodes to campaigns has nowhere to put this at all, and the Job
                    # sits Pending until its three-hour timeout. The toleration is what
                    # gets it onto those nodes; `await_admission` is what waits until one
                    # of them has room.
                    # One tree, written by containers that run as different users -- see
                    # CAMPAIGN_TREE_GID. supplementalGroups covers an execution image whose
                    # own user is not the family's.
                    "securityContext": {"fsGroup": CAMPAIGN_TREE_GID,
                                        "supplementalGroups": [CAMPAIGN_TREE_GID]},
                    "tolerations": list(CAMPAIGN_NODE_TOLERATIONS),
                    **({"imagePullSecrets": [{"name": pull_secret_name}]}
                       if pull_secret_name else {}),
                    "volumes": volumes,
                    "initContainers": init_containers,
                    "containers": containers,
                },
            },
        },
    }


def await_job(core, batch, cluster_config, campaign_id: str, namespace: str, name: str,
              timeout: int = _DEFAULT_TIMEOUT,
              batch_commands=None) -> tuple:
    """Wait for the postprocessing Job *name* and return its ``(ok, message)``.

    A pure waiter: it creates nothing, replaces nothing and deletes nothing, so it is
    equally correct for the attempt that created the Job and for a process that only found
    it running. That is what lets the submit path and the re-attach path share one
    definition of a verdict -- two waiters would be two answers to "did this postprocess
    succeed?", and a campaign only has one record to write them into.

    ``ok`` is three-valued: ``True`` the Job was read as succeeded, ``False`` it was read
    as failed, ``None`` **this process can no longer see what the Job is doing** -- the API
    server would not answer, or the deadline passed while the Job was still active. The
    Job is a cluster object that outlives any waiter, so a waiter that loses sight of it
    has learned nothing; ``None`` keeps that apart from a failure so the campaign keeps
    whatever it already says instead of being marked failed over a conversion that may be
    finishing.

    *batch_commands* names what the Job was asked to do, and so what its success means: a
    batch Job has derived one batch's tables and completed no campaign.
    """
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    deadline = time.time() + timeout
    # Published from here because this is the only place that knows the Job is still
    # running. Nothing the pod writes leaves it until it exits, so without this the
    # POSTPROCESSING section stays empty for the whole of a conversion measured in
    # minutes -- and the only way to watch one is a pod name nobody off-cluster has.
    next_live_log = 0.0
    while time.time() < deadline:
        if time.time() >= next_live_log:
            publish_live_log(core, cluster_config, campaign_id, namespace, name)
            next_live_log = time.time() + _LIVE_LOG_INTERVAL
        try:
            status = batch.read_namespaced_job_status(
                name=name, namespace=namespace).status
        except ApiException as e:
            if e.status == 404:  # reaped (ttl) — treat as finished
                return True, "postprocessing job finished (reaped)"
            # Unknown, not failed: the Job is a cluster object and keeps converting
            # while this process cannot read it. What is lost here is the observation,
            # and an observation that did not happen is not a negative result.
            return None, (f"could not read the status of postprocessing job {name}, "
                          f"so what it did is unknown; the Job may still be running: "
                          f"{e}")
        if not status.active:
            if status.succeeded:
                logger.info("Postprocessing job %s succeeded", name)
                return True, ("batch postprocessing complete" if batch_commands is not None
                              else "postprocessing complete")
            if status.failed:
                # The only place a postprocess is reported as failed, because it is the
                # only place a failure was actually READ: `backoffLimit: 0` makes one
                # container exit terminal, so this field is the Job controller's own
                # verdict. Every other exit from this loop is an unknown.
                #
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
                f"postprocessing job {name} cannot start: {blocked}. Its containers "
                f"run the controller image and, where the campaign has rosbags, the "
                f"campaign's own execution image, so this is about pulling or "
                f"scheduling those -- not about postprocessing, which has not run. "
                f"Nothing about the campaign's results is wrong; re-run "
                f"postprocessing once the pod can start.")
        time.sleep(_POLL_SECONDS)
    # The deadline is this process's patience, not a verdict about the Job: nothing here
    # stops it, and a conversion measured in hours is still running when the wait gives
    # up. Reported as unknown so the campaign keeps whatever it already says about
    # postprocessing, and re-reading the Job (a retrigger) settles it.
    return None, (f"stopped waiting for postprocessing job {name} after {timeout}s; "
                  f"it was still running, so its outcome is unknown -- re-run "
                  f"postprocessing to read it")


def campaign_job_name(campaign_id: str) -> str:
    """The name of *campaign_id*'s campaign-level postprocessing Job.

    Deterministic from the campaign id alone, because the campaign-level postprocess passes
    no discriminator (see :func:`run_conversion_job`) -- which is what makes a Job found
    running attributable to the campaign whose record it is owed to. A discriminated Job is
    a search's per-batch conversion: it answers to its batch's driver and to no campaign
    record, so an equality test against this name is what keeps one from being written into
    the campaign's postprocessing verdict.
    """
    return _short_job_name("robovast-postproc-", campaign_id)


def live_campaign_jobs(namespace: str, kube_context=None) -> dict:
    """``{label-safe campaign: job name}`` for every postprocessing Job still active.

    One labelled listing, not a read per campaign: the Job carries
    ``jobgroup=postprocessing`` and its campaign's label-safe id (see
    :func:`build_manifest`), so what is running can be asked of the cluster rather than
    guessed from the set of campaigns anyone happens to know about.

    The label is the *sanitized* id and several ids can sanitize to one label, so the
    caller resolves it against the campaigns it knows and confirms the name with
    :func:`campaign_job_name`. Returns empty when the cluster cannot be listed: a Job that
    cannot be read is not a Job whose outcome anyone may record.
    """
    from kubernetes import client  # noqa: PLC0415

    from .kube_client import load_kube_config  # noqa: PLC0415

    load_kube_config(kube_context)
    jobs = client.BatchV1Api().list_namespaced_job(
        namespace=namespace, label_selector=f"jobgroup={POSTPROCESS_JOBGROUP}")
    live = {}
    for job in getattr(jobs, "items", None) or []:
        if not getattr(getattr(job, "status", None), "active", None):
            continue
        labels = getattr(job.metadata, "labels", None) or {}
        campaign = labels.get("campaign-id")
        if campaign:
            live[campaign] = job.metadata.name
    return live


def reattach_conversion_job(cluster_config, campaign_id: str, campaign_root: str,
                            namespace: str, job_name: str,
                            timeout: int = _DEFAULT_TIMEOUT,
                            kube_context=None) -> tuple:
    """Wait for a postprocessing Job this process did not submit. ``(ok, message)``.

    Returns ``ok is None`` when the Job could not be confirmed live, and then *message*
    says why. That third answer is the point of this function: the Job outlives the service
    process, so the only thing worse than not recording its outcome is recording one it did
    not have -- a campaign whose conversion succeeded must not be marked failed because the
    API server was briefly unreadable.

    Nothing is created or replaced. The Job already mounts the scripts it was created with,
    and the kubelet syncs a ConfigMap's new content into every mount of it, so writing them
    again would swap the script out from under the running interpreter (see
    :func:`run_conversion_job`).
    """
    from kubernetes import client  # noqa: PLC0415

    from robovast.common.errors import ClusterUnreachableError  # noqa: PLC0415

    from .kube_client import load_kube_config  # noqa: PLC0415

    # Explicit, for the reason the submit path loads it explicitly: these clients read
    # whatever context is loaded when they are constructed, and the campaign's Jobs went to
    # the service's --context cluster rather than the ambient kubeconfig.
    load_kube_config(kube_context)
    core = client.CoreV1Api()
    batch = client.BatchV1Api()
    try:
        with api_transport_errors("re-attaching to the postprocessing job"):
            if not _live_job(batch, namespace, job_name):
                return None, (f"postprocessing job {job_name} is no longer active, so this "
                              f"process has no outcome to record for {campaign_id}")
    except ClusterUnreachableError as e:
        return None, f"the postprocessing job {job_name} could not be read: {e}"
    logger.info("Re-attached to the postprocessing job %s already in flight; its scripts "
                "are untouched", job_name)
    ok, message = await_job(core, batch, cluster_config, campaign_id, namespace, job_name,
                            timeout=timeout)
    return record_job_outputs(cluster_config, campaign_id, campaign_root, ok, message)


def run_conversion_job(cluster_config, campaign_id: str, namespace: str, image,
                       rosbag_cmds: list, force: bool = False,
                       timeout: int = _DEFAULT_TIMEOUT, kube_context=None,
                       discriminator: str = "", tolerate_under=(), skip=None,
                       batch_commands=None, convert_resources=None,
                       admission=None) -> tuple:
    """Create the postprocessing Job and wait for it. Returns ``(ok, message)``.

    ``ok`` is three-valued, and the third value is the point of it: ``True`` the Job was
    read as succeeded, ``False`` it was read as failed, ``None`` **this process can no
    longer see what the Job is doing** -- the API server would not answer, or the wait
    reached its deadline while the Job was still active. ``None`` is not a synonym for
    ``False``: the Job runs in the cluster and outlives this process, so a driver that
    loses sight of it has learned nothing about the conversion. Recorded as a failure it
    sends someone to redo hours of work over a conversion that finished, and marks a
    campaign whose derived data is complete as carrying none.

    *image* is the campaign's execution image, and is needed only for the conversion: an
    empty *rosbag_cmds* builds a Job that never pulls it. Callers that cannot know in
    advance whether it is needed should pass ``None`` and let *rosbag_cmds* decide, so a
    campaign whose image has gone from the registry still postprocesses.

    *batch_commands* makes this a per-batch Job -- see :func:`build_manifest`. With an
    empty list and nothing to convert there is no work at all, which is a no-op success.

    *admission* is the deployment's queue. Given, this pod waits for room like every other
    pod on the cluster rather than being created against a cluster that has none -- see
    :func:`await_admission`. ``None`` creates it directly, which is what a lane with no queue
    (a local service, an off-cluster driver) must do.

    *discriminator* names WHICH conversion of this campaign this is, and must be set by
    any caller that converts the same campaign more than once -- a search, which converts
    once per repetitions-group. Without it the second create returns 409 and the wait below
    reads the FIRST conversion's already-completed Job, returning "rosbag conversion
    complete" having converted nothing. Left empty the Job keeps the one-shot
    campaign-level name, where the 409 fallthrough is right: a retry of a single conversion
    should wait on the Job already in flight rather than launch a second copy.
    """
    if not rosbag_cmds and batch_commands is not None and not batch_commands:
        return True, "no rosbag conversion configured; nothing to run"
    if rosbag_cmds and not image:
        # Refused rather than defaulted: converting in the wrong image deserializes the
        # campaign's custom message types against a stranger's definitions, and what comes
        # out of that is wrong data rather than an error.
        return False, ("no execution image for the campaign's rosbag conversion; its "
                       "custom ROS2 types deserialize in no other image")

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
        discriminator=discriminator, tolerate_under=tolerate_under, skip=skip,
        batch_commands=batch_commands, convert_resources=convert_resources)
    name = manifest["metadata"]["name"]

    # Whether a Job of this name is already running is decided HERE, ahead of every write,
    # and that order is load-bearing. The Job name comes from the campaign, so a second
    # attempt -- a retrigger, or a service restart resuming the campaign -- meets the first
    # attempt's Job still converting. Its conversion container has the scripts ConfigMap
    # below mounted at /scripts and is executing out of that mount, and the kubelet syncs a
    # ConfigMap's new content into every mount of it: writing the ConfigMap swaps the script
    # out from under the running interpreter, which exits 1, and deleting it on the way out
    # takes the mount away entirely. Either one destroys a healthy conversion, and the
    # attempt that did it is the one that then reports the failure as the campaign's.
    #
    # So a live Job is adopted with nothing written: it already carries the scripts it was
    # created with, generated by this same driver package, so there is nothing this attempt
    # could add. Only an attempt that creates or re-creates the Job owns what it mounts.
    #
    # First call to touch the API server, so it is where an unreachable cluster surfaces.
    # Reported as a reason on the campaign's postprocessing_error and re-runnable once the
    # cluster is back -- the runs themselves are already published -- rather than reaching
    # the caller as a urllib3 traceback.
    try:
        with api_transport_errors("submitting the postprocessing job"):
            adopted = _live_job(batch, namespace, name)
    except ClusterUnreachableError as e:
        return False, f"postprocessing cannot be scheduled: {e}"

    # Queued for capacity BEFORE anything is written, and only when this attempt is the one
    # creating the Job: an adopted Job already holds real capacity on a real node, so
    # admitting it a second time would charge the cluster twice for one pod.
    admitted = False
    if admission is not None and not adopted:
        granted, node_id, message = await_admission(admission, campaign_id, name, manifest,
                                                    timeout=timeout)
        if not granted:
            return False, message
        admitted = True
        _pin_to(manifest, node_id)

    # The conversion scripts arrive as a per-campaign ConfigMap mounted at /scripts —
    # the driver's own copy, so no controller-image version skew. Create it before the
    # Job (the pod waits in ContainerCreating until the volume source exists) and delete
    # it once the Job is done. Only where something mounts it: a Job with no conversion
    # container declares no such volume, and creating the ConfigMap anyway would leave one
    # behind for every host-only postprocess.
    #
    # `owned_cm_name` is the ConfigMap this attempt created or replaced, and it is the only
    # thing the cleanup below deletes -- deletion follows creation, explicitly, so that an
    # adopting waiter cannot remove the scripts of the Job it is waiting on.
    owned_cm_name = ""
    if rosbag_cmds and not adopted:
        cm = scripts_configmap_manifest(campaign_id, namespace, discriminator=discriminator)
        cm_name = cm["metadata"]["name"]
        try:
            with api_transport_errors("submitting the postprocessing job"):
                try:
                    core.create_namespaced_config_map(namespace=namespace, body=cm)
                except ApiException as e:
                    if e.status == 409:  # a stale copy from a prior run — replace it
                        core.replace_namespaced_config_map(name=cm_name,
                                                           namespace=namespace, body=cm)
                    else:
                        return False, ("could not create postprocessing scripts "
                                       f"ConfigMap: {e}")
            owned_cm_name = cm_name
        except ClusterUnreachableError as e:
            return False, f"postprocessing cannot be scheduled: {e}"

    try:
        if adopted:
            logger.info("Waiting on the postprocessing job %s already in flight; its "
                        "scripts are untouched", name)
        else:
            try:
                # Wrapped even though the read above gets there first on every reachable
                # cluster: a cluster can go away between the two, and then this is where an
                # unreachable one has to surface.
                with api_transport_errors("submitting the postprocessing job"):
                    batch.create_namespaced_job(namespace=namespace, body=manifest)
            except ClusterUnreachableError as e:
                return False, f"postprocessing cannot be scheduled: {e}"
            except ApiException as e:
                if e.status != 409:
                    return False, f"could not create postprocessing job: {e}"
                # 409 with no live Job seen above: either a FINISHED Job of this name is
                # still here -- the name is derived from the campaign, so waiting on it
                # would report an earlier attempt's outcome as this attempt's, against a
                # pod whose containers ran a previous version of this script -- or a Job
                # started between that read and this create. Replace the finished one;
                # adopt the one that raced us, and drop ownership of the ConfigMap with it,
                # because from here on this attempt is a waiter on someone else's Job.
                outcome = _adopt_or_replace(batch, namespace, name, manifest)
                if not outcome:
                    return False, (f"postprocessing job {name} already exists and could "
                                   f"not be replaced; retry once it has been removed")
                if outcome == _JOB_ADOPTED:
                    owned_cm_name = ""
            logger.info("Postprocessing job %s created (conversion image=%s)", name,
                        image if rosbag_cmds else "none needed")

        return await_job(core, batch, cluster_config, campaign_id, namespace, name,
                         timeout=timeout, batch_commands=batch_commands)
    finally:
        # Release the reservation the moment the pod is gone, so what it held is spendable on
        # the next drain. In the `finally` because every exit from here -- finished, failed
        # or timed out -- ends the pod's claim, and a reservation left behind would shrink
        # the cluster by a pod that no longer exists for as long as this service runs.
        if admitted:
            admission.finished(name)
        # Best-effort cleanup of the scripts ConfigMap this attempt created (labeled for
        # manual sweep if the driver dies before this runs). The Job's own
        # ttlSecondsAfterFinished reaps it. Empty for an adopted Job: those scripts are
        # mounted in a container this attempt did not start, and deleting them out from
        # under it is how a waiter breaks the conversion it is waiting on.
        if owned_cm_name:
            try:
                core.delete_namespaced_config_map(name=owned_cm_name, namespace=namespace)
            except ApiException as e:
                if e.status != 404:
                    logger.warning("Could not delete scripts ConfigMap %s: %s",
                                   owned_cm_name, e)
