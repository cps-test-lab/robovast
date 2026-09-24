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
A pod cannot bind-mount the service's results volume, so in-cluster postprocessing runs
as a **Job** and this module builds/creates/tracks it.

One pod, one copy of the data. A ``campaign`` ``emptyDir`` mounted at
:data:`CAMPAIGN_MOUNT` in every container holds the campaign tree; the containers are
ordered by Kubernetes alone, because initContainers run sequentially to completion in
declaration order before the regular containers start:

* ``stage`` (initContainer, sidecar image) fetches the campaign as one tar stream from the
  service's data plane (``GET /data/campaigns/<id>/archive``, narrowed by
  :func:`~robovast.execution.campaign_archive.stage_include`) and lands it on the shared
  mount: ``curl | tar``, nothing of ours in between.
* ``convert`` (initContainer, the campaign's execution image) runs the ``rosbags_*`` →
  CSV step, reading and writing that mount only. It exists **only when the campaign
  declares a plugin needing the execution image** — rosbags carry the system under
  test's *custom ROS2 message types* and only deserialize in its image, which is also
  why nothing else may be asked of that image.
* ``host`` (container, controller image) runs everything after the conversion — the
  derived tables, and for a campaign-level Job the index ingest and metadata — and is what
  delivers the results: one tar of what the Job derived, ``PUT`` to the data plane, which
  writes it into the campaign's directory on the service's results volume. It is the
  pod's main container in both shapes of this Job, because it is the only one given the
  campaign's token: a Job that ended at its conversion could send nothing anywhere. A
  per-batch Job (a search's) runs it without the completing steps.

The campaign's directory on the service is what the submitting process reads the Job's
inputs from and what the Job's outputs land in; nothing is fetched into this process and
nothing synced out of it.

**Nothing is baked into the execution image.** The conversion scripts are mounted in
from a per-campaign ConfigMap (the K8s analog of ``-v $SCRIPT_DIR:/scripts:ro``), so
the scripts always match the driver that generated the command.
"""

import dataclasses
import datetime
import hashlib
import json
import logging
import os
import re
import time
from urllib.parse import quote

from robovast.common.execution import resolve_controller_image, resolve_sidecar_image
from robovast.execution.campaign_archive import in_part
from robovast.common.quantity import to_bytes, to_cores
from robovast.common.stop import sleep_unless_stopped

from . import pod_access, postprocess_usage
from .kube_client import api_transport_errors
from .campaign_job import campaign_job_manifest, pin_campaign_job

logger = logging.getLogger(__name__)

#: ``jobgroup`` label every postprocessing Job carries. Named rather than repeated because
#: it is a contract in two directions: the manifest sets it and :func:`live_campaign_jobs`
#: selects on it, and a Job created without it is a Job no later service process can find
#: still running.
POSTPROCESS_JOBGROUP = "postprocessing"

#: Where the campaign tree lives inside the Job's pod, mounted from one ``emptyDir`` into
#: every container. There is exactly one copy of the data in the pod: the stage container
#: writes it, the conversion reads and writes it in place at campaign-relative paths, and
#: the host container reads it and delivers what changed. Separate input and output trees
#: would mean either a second copy of the run data or a merge step, and the
#: campaign-relative paths are what let outputs land straight at their canonical paths.
CAMPAIGN_MOUNT = "/campaign"

#: Group every container in this pod shares, so one campaign tree can be written by all of
#: them. They do not share a user: the sidecar and controller images run as root and an
#: execution image runs as its own unprivileged user (1000 for the family's), and the tree
#: is created by one and written by the other. ``fsGroup`` makes the kubelet group-own the
#: shared volume and set setgid on it; the stage container then hands the tree it extracts
#: to this group explicitly, because ``tar`` run as root restores the archive's owner and
#: mode and so bypasses both the setgid bit and the umask.
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


def postprocess_owner(campaign_id: str) -> str:
    """The admission queue's owner for this campaign's postprocessing.

    Its own owner rather than the campaign's, so a postprocess queues behind that
    campaign's trials rather than among them -- and one name whether the postprocess is one
    Job or a split's parts.
    """
    return f"{campaign_id}{_POSTPROCESS_OWNER_SUFFIX}"

POLL_SECONDS = 5
DEFAULT_TIMEOUT = 3 * 60 * 60

#: What a caller is told when the campaign was stopped while its postprocessing waited for
#: capacity. Its own message because nothing failed and nothing is wrong with the cluster:
#: the derived data is simply missing, and a re-run is all it takes.
POSTPROCESSING_QUEUE_CANCELLED = (
    "postprocessing was cancelled while queued for capacity; the runs' results are "
    "complete and re-running postprocessing derives the rest.")

#: Disk the pod reserves and may use, for every step. Not settable by a campaign.
#:
#: The request is what keeps a campaign's worth of staged data from landing on a node that
#: reserved no disk for it -- without it the node hits disk pressure and evicts the campaign
#: pods running beside it. It stays split from the limit, unlike cpu and memory: disk is
#: reclaimed as the conversion writes its outputs and the staged bags are dropped, so a
#: ceiling near the reservation would fail a large campaign that never held that much at once,
#: while a reservation near the ceiling would price every postprocessing pod at a disk figure
#: almost none of them reach.
#: The smallest disk a postprocessing step asks for, and what a step asks for when its need
#: is not known. Not "the request": the stage step derives its own from what it will fetch.
POSTPROCESS_EPHEMERAL_FLOOR = "20Gi"

#: The most disk one postprocessing pod may claim on a node it shares with trials. Caps the
#: stage step's derived figure, and is the ceiling every step is held to.
#:
#: **Disk is the one resource here whose request and ceiling are deliberately not equal**,
#: unlike cpu and memory (see :func:`step_resources`). What a step *downloads* is ours to
#: know and is what the request describes; what it *writes* into the shared mount is the
#: campaign's, declared in its ``.vast`` -- a conversion may emit a few CSVs or encode video
#: of every run, and a ``command:`` step may write anything at all. Holding a pod to a
#: reservation that cannot account for that would evict campaigns for declaring the
#: postprocessing they are entitled to declare. Same reason ``POSTPROCESS_HOST_FLOOR`` is
#: raised by a campaign's own figure and never lowered by it.
POSTPROCESS_EPHEMERAL_CAP = "200Gi"

#: Headroom over the bytes the stage step will fetch, for what lands in the same mount but is
#: not part of the archive it extracted: the conversion's own outputs, and the filesystem's
#: per-file overhead across a campaign's many small files.
STAGE_EPHEMERAL_HEADROOM = 1.5


def stage_bytes(campaign_root: str, skip_bags: bool, batch_jobs: str, part: str = "") -> int:
    """Bytes the stage step will extract, read from the campaign's directory.

    The archive the pod fetches is the service's own tree narrowed by
    :func:`~robovast.execution.campaign_archive.stage_include`, so the same selection over
    the same tree is what it will cost on the node: a campaign whose pod opens no bag does
    not stage them, and a per-batch Job stages one batch's job artifacts. One metadata-only
    walk (:func:`~robovast.execution.campaign_archive.campaign_source_bytes`), on a path
    that is already creating a Job; a directory that vanishes under the walk costs the
    figure some accuracy and the submit nothing.
    """
    from robovast.execution import campaign_archive  # noqa: PLC0415

    staged = campaign_archive.stage_include(skip_bags=skip_bags, batch_jobs=batch_jobs)
    include = staged
    if part:
        within_part = campaign_archive.part_include(campaign_root, part)

        def include(rel, is_dir):  # pylint: disable=function-redefined
            return staged(rel, is_dir) and within_part(rel, is_dir)
    return campaign_archive.campaign_source_bytes(campaign_root, include=include)


def stage_ephemeral_request(stage_bytes) -> str:
    """The stage step's ``ephemeral-storage`` request for a campaign of *stage_bytes*.

    The scheduler places the pod on this figure and the kubelet evicts against it, so it has
    to describe the staged tree rather than a typical one: a campaign is written to the node's
    disk in full, and what that costs is the size of the campaign. The floor still applies --
    it is what a small campaign asks for -- and the limit still caps it, because a request
    above its own limit is not a pod spec Kubernetes accepts.

    ``None`` means the size is not known; the floor then stands.
    """
    floor = to_bytes(POSTPROCESS_EPHEMERAL_FLOOR)
    ceiling = to_bytes(POSTPROCESS_EPHEMERAL_CAP)
    want = floor if not stage_bytes else int(stage_bytes * STAGE_EPHEMERAL_HEADROOM)
    gib = 1 << 30
    return f"{max(floor, min(want, ceiling)) // gib}Gi"


def step_resources(cpu, memory, ephemeral: str = "") -> dict:
    """One step's ``resources``, with cpu and memory as reservation *and* ceiling.

    **The equality is the point, and it is about comparability rather than thrift.** This pod
    runs on the nodes that run trials, so a step allowed past its reservation takes cores from
    a run whose own request was honest -- and that run's timing becomes a function of which
    campaign happened to be postprocessing beside it. That is precisely the hidden variable
    the CPU governor work exists to remove, reintroduced from a direction nothing downstream
    looks at: no artifact of the affected run records that a conversion was running.

    Nothing here is under test, so the throughput given up is real and the measurement it
    protects is worth more.

    **Disk is stated as a pair instead**, and the asymmetry is not an oversight: see
    :data:`POSTPROCESS_EPHEMERAL_CAP`. A step's reservation describes what RoboVAST will
    download, which it knows; its ceiling has to cover what the campaign's own declared
    postprocessing writes beside it, which it does not.
    """
    quantities = {"cpu": str(cpu), "memory": str(memory)}
    request = ephemeral or POSTPROCESS_EPHEMERAL_FLOOR
    return {
        "requests": dict(quantities, **{"ephemeral-storage": request}),
        "limits": dict(quantities, **{"ephemeral-storage": POSTPROCESS_EPHEMERAL_CAP}),
    }


def stage_resources(stage_bytes=None) -> dict:
    """What the stage step gets for a campaign of *stage_bytes*.

    **cpu and memory are fixed, and a campaign's figure does not raise them** -- unlike the
    host step below. Staging is ``curl | tar``: the archive streams through a pipe and each
    member is written as it arrives, so what it holds *in memory* is set by that construction
    and not by the size of the campaign. The small memory bound is therefore a GUARD rather
    than a reservation, and it runs nothing a ``.vast`` would know the appetite of better
    than we do.

    **Disk is the opposite, because that is where the streaming ends.** Every member lands on
    the node's filesystem and stays there for the pod's life, so the staged tree *is* the
    campaign and ephemeral-storage is the one figure here that has to scale with it. Left
    fixed it describes a typical campaign rather than this one, and a campaign larger than
    the figure is scheduled onto a node that cannot hold it and evicted partway through --
    losing the whole postprocessing, not the excess.
    """
    return step_resources(2, "512Mi", ephemeral=stage_ephemeral_request(stage_bytes))


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
                     memory=int(_charge("memory", to_bytes)),
                     ephemeral=int(_charge("ephemeral-storage", to_bytes)))


def await_admission(admission, campaign_id: str, name: str, manifest: dict,
                    timeout: float = DEFAULT_TIMEOUT, poll: float = POLL_SECONDS,
                    should_stop=None) -> tuple:
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
    from .node_admission import campaign_start_key, describe_resources  # noqa: PLC0415

    sizing = pod_sizing(manifest)
    asked = describe_resources(sizing.cpu, sizing.memory, sizing.ephemeral)
    granted = {}

    try:
        # Permanent, so it raises rather than waits: a pod larger than any node in the
        # cluster is a figure to change, and waiting for a machine that does not exist would
        # hold the campaign's results forever with nothing said.
        admission.preflight(sizing)
    except AdmissionRefused as exc:
        return False, None, (
            f"postprocessing needs {asked} and no node in this cluster is that large. Lower "
            f"results_processing.resources for this campaign; a disk figure is what the "
            f"campaign stages and is not set there. ({exc})")

    def _record_grant(node_id):
        granted["node_id"] = node_id

    owner = postprocess_owner(campaign_id)
    admission.submit(owner, [(name, sizing, _record_grant)],
                     started_at=campaign_start_key(campaign_id),
                     priority=POSTPROCESS_PRIORITY, campaign=campaign_id)

    deadline = time.monotonic() + timeout
    logged = 0.0
    while time.monotonic() < deadline:
        if should_stop is not None and should_stop():
            # The submission goes with the wait. Left in the queue it would be granted room
            # later and create a pod for a campaign nobody is waiting on -- capacity spent
            # on work that was cancelled, which is what the queue exists to prevent.
            admission.finished(name)
            return False, None, POSTPROCESSING_QUEUE_CANCELLED
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
        sleep_unless_stopped(poll, should_stop)

    reason = admission.refusal(owner)
    admission.finished(name)
    from .node_admission import DISK_WAIT  # noqa: PLC0415
    if reason.startswith(DISK_WAIT):
        # Not the cluster being full: nothing is admitted while the disk the results land on
        # is below its reserve, and that wants space freed rather than smaller resources.
        return False, None, (
            f"postprocessing waited {timeout:g}s and was not started: "
            f"{reason[len(DISK_WAIT):]} The campaign's runs are complete; delete campaigns "
            f"no longer needed, then re-run postprocessing.")
    return False, None, (
        f"postprocessing waited {timeout:g}s for {asked} and the cluster stayed full. The "
        f"campaign's runs are complete; re-run postprocessing when there is room, or lower "
        f"results_processing.resources.")


def image_commands_for(campaign_root: str, skip=None, skip_rosout: bool = False) -> list:
    """The steps of a campaign's postprocessing that run in its execution image, in order.

    Taken from the same list the local lane runs
    (:func:`~robovast.results_processing.postprocessing.campaign_postprocessing_commands`),
    so the Job's image container runs exactly what ``vast campaign postprocess`` would run in
    a container, and the host step then runs the rest. Which steps those are is each
    plugin's own answer (``needs_execution_image``), not a list kept here. Empty when the
    campaign has none -- then the Job has no image container.
    """
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        campaign_postprocessing_commands, needs_execution_image)

    vast_path = campaign_vast(campaign_root)
    config_dir = os.path.dirname(vast_path)
    return [command for command in
            campaign_postprocessing_commands(vast_path, skip=skip, skip_rosout=skip_rosout)
            if needs_execution_image(command, config_dir)]


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

    from robovast.common.campaign_data import (campaign_image_record,  # noqa: PLC0415
                                               image_is_pullable)
    from robovast.common.config import SCENARIO_CONTAINER  # noqa: PLC0415

    path = os.path.join(str(campaign_dir), "_execution", "execution.yaml")
    # No pre-check that execution.yaml exists. It is the RICHEST record, not the only one:
    # campaign_image_record falls back to launch.yaml, which is written before the first job
    # and therefore survives a campaign whose execution record was never written. Refusing on
    # the file's absence would strand such a campaign -- every run on disk, every bag intact,
    # and no way to convert them -- so absence is left to the "no image recorded anywhere"
    # check below, which is the condition that actually matters.
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


def _write_failure_log(campaign_id: str, log_path: str, message: str) -> None:
    """Write the POSTPROCESSING phase file when nothing else has.

    The phase file IS the section: every surface assembles the campaign log from the files
    that exist (``campaign_logs.INFRA_PHASES``), so a Job whose pod's log could not be read
    would leave a campaign with no POSTPROCESSING section at all -- the reader sees the
    phases stop after RUN, with the failure reported only in a status field elsewhere.
    Writing the account here is what makes such a failure visible where a successful one is
    read.

    Only where no log arrived: a failed Job's pod is read one last time before its verdict
    is returned (:func:`await_job`), so this is reached when the pod itself was gone or
    the API would not answer for it.
    """

    # The message still carries POINTER_SLOT: the pointer is decided after this file is
    # written, precisely BY whether it was. Inside the file the slot has nothing to say --
    # the reader is already in the section it would point at.
    headline = message.replace(POINTER_SLOT, "").strip()
    lines = [
        f"Postprocessing failed: {headline}",
        "",
        "No log could be read from the Job's pod, so this is the whole account. The Job "
        "first stages the campaign's recorded run data into the pod and then, where the "
        "campaign needs it, converts its rosbags; only after both does the step that "
        "writes this log run.",
        "",
        "Which container failed, and how, is in the line above where the pod could still "
        "say: each container's exit status is carried in the pod's status whatever "
        "happened to the container, and a container the kubelet killed under node disk "
        "pressure runs no cleanup at all. Node disk and the service's data plane are what "
        "to check.",
    ]
    text = "\n".join(lines) + "\n"
    logger.warning("Postprocessing failed and its pod left no log; recording the account "
                   "for %s", campaign_id)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        logger.warning("Could not write the postprocessing log for %s: %s", campaign_id, e)


def campaign_vast(campaign_root) -> str:
    """The campaign's ``.vast`` (``<campaign>/_config/<name>.vast``) — the same single
    source of truth the service edits in place, so the cluster conversion Job runs
    exactly the config the re-run dialog saved.
    """
    from robovast.common.results_utils import campaign_vast as _campaign_vast  # noqa: PLC0415

    return str(_campaign_vast(campaign_root))


def _read_submit_inputs(campaign_root: str, skip=None, skip_rosout: bool = False) -> tuple:
    """``(image_cmds, image, tolerate_under, convert_resources, split)`` from the campaign
    tree.

    The five facts a postprocess needs about a campaign, and all five come from files in
    its directory on the service: the ``.vast`` says which steps run in the execution image,
    how much they may use and which of them can be split across Jobs (:func:`_plan_split`),
    ``execution.yaml`` names that image, and the intervention ledger names the runs whose
    output was cut short mid-write.
    """
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        postprocess_convert_resources)
    from robovast.results_processing.postprocessing_plugins import (  # noqa: PLC0415
        _interrupted_job_dirs)

    vast_path = campaign_vast(campaign_root)
    # A host-only campaign can still be split: its run-scoped host steps are a map too.
    split = _plan_split(campaign_root, vast_path, skip=skip, skip_rosout=skip_rosout)
    image_cmds = image_commands_for(campaign_root, skip=skip, skip_rosout=skip_rosout)
    if not image_cmds:
        # No image is resolved at all for a host-only campaign: nothing in the pod pulls
        # one, so a campaign whose execution image has since gone from the registry still
        # postprocesses. The sizing goes the same way: with no conversion container there is
        # nothing for it to size.
        return [], None, (), None, split
    # The same seam the local lane reads, for the same reason: a bag belonging to a job
    # that was stopped by hand or invalidated by the runner cannot be opened, ever, and
    # must not fail the conversion for every job that finished.
    return (image_cmds, campaign_execution_image(campaign_root),
            tuple(_interrupted_job_dirs(campaign_root)),
            postprocess_convert_resources(vast_path), split)


def postprocess_campaign(cluster_config, campaign_id: str, campaign_root: str,  # pylint: disable=unused-argument
                         namespace: str, *, token: str, force: bool = False, skip=None,
                         skip_rosout: bool = False, kube_context=None, state=None,
                         admission=None, should_stop=None) -> tuple:
    """Analysis postprocessing for one campaign, in-cluster. Returns ``(ok, message)``.

    ``ok`` carries :func:`run_conversion_job`'s three values through unchanged, ``None``
    among them: this process losing sight of the Job is not the Job failing, and the
    persisting callers key off ``None`` to leave the campaign's recorded outcome alone.

    The single implementation behind both entry points — the per-campaign controller
    (auto-chain) and the service (explicit re-run). All of the work happens in the Job: it
    stages the campaign into its pod, converts rosbags where the campaign asks for it, runs
    the host steps (index ingest, metadata) in its last container, and delivers what it
    derived to the data plane, which writes it into *campaign_root*.

    *campaign_root* is the campaign's directory on the service's results volume -- where
    the manifest's inputs are read from (:func:`_read_submit_inputs`) and where the Job's
    outputs land. *token* is the campaign's scoped data-plane token
    (``service.scoped_token(pod_access.campaign_scope(campaign_id))``), which the pod is
    given through its Secret. *cluster_config* answers one question, the pull Secret the
    pod's own images need (:func:`~.cluster_execution.resolve_pull_secret`).

    *kube_context* must be the same context the campaign's Jobs were submitted with;
    ``None`` means the active kubeconfig context, which is only correct when the caller
    has none of its own.

    *state* is accepted for the caller's call shape and stays empty on this lane: every
    step runs in a pod, so a step's line reaches this process through the pod's log
    (:func:`publish_live_log`) rather than a live ``stage`` marker fed from here.

    *should_stop*, when the caller has one, ends this early for a campaign that was
    stopped: the Job is deleted and the campaign is left saying that its derived data was
    not computed. Every step runs in the pod, so deleting it is the whole cancellation --
    and every step is restartable, which is what makes deleting one mid-flight safe: a bag
    records itself as converted only once its handlers have finished, and the index ingest
    clears a campaign's rows before writing them, so a re-run replaces a partial load
    rather than doubling it. What a cancelled campaign never has is the provenance record
    that says it carries derived data, because that is written after everything else.
    """
    image_cmds, image, tolerate_under, convert_resources, split = _read_submit_inputs(
        campaign_root, skip=skip, skip_rosout=skip_rosout)
    if split is not None:
        return _postprocess_split(
            cluster_config, campaign_id, campaign_root, namespace, split, image,
            token=token, force=force, skip=skip, kube_context=kube_context,
            tolerate_under=tolerate_under, convert_resources=convert_resources,
            admission=admission, should_stop=should_stop)
    if not image_cmds:
        logger.info("Campaign %s has no step in its execution image; the Job runs its host "
                    "steps only, and stages the campaign without its rosbags", campaign_id)
    ok, message = run_conversion_job(
        cluster_config, campaign_id, campaign_root, namespace, image, image_cmds, token=token,
        force=force, kube_context=kube_context, tolerate_under=tolerate_under, skip=skip,
        convert_resources=convert_resources, admission=admission,
        should_stop=should_stop)
    return record_job_outputs(campaign_id, campaign_root, ok, message,
                              should_stop=should_stop)


def _plan_split(campaign_root: str, vast_path: str, skip=None, skip_rosout: bool = False):
    """``(map_cmds, reduce_cmds, parts)`` when this campaign's postprocessing is split, or
    ``None`` to run it in one Job: no run-scoped steps, a cap of one
    (``ROBOVAST_POSTPROCESS_MAX_PARALLEL``), or fewer than two units of work.

    How many parts is the cluster's answer unless an operator capped it
    (:func:`~.postprocess_parts.max_parallel`), asked with what one conversion of THIS
    campaign reserves -- its ``results_processing.resources``."""
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        campaign_postprocessing_commands, postprocess_convert_resources,
        split_postprocessing)

    from .postprocess_parts import max_parallel, plan_parts  # noqa: PLC0415

    limit = max_parallel(convert_cpu=postprocess_convert_resources(vast_path)["cpu"])
    if limit < 2:
        return None
    map_cmds, reduce_cmds = split_postprocessing(
        campaign_postprocessing_commands(vast_path, skip=skip, skip_rosout=skip_rosout),
        os.path.dirname(vast_path))
    if not map_cmds:
        return None
    parts = plan_parts(campaign_root, limit)
    if not parts:
        return None
    return map_cmds, reduce_cmds, parts


def _postprocess_split(cluster_config, campaign_id: str, campaign_root: str, namespace: str,
                       split, image, *, token: str, force: bool, skip, kube_context,
                       tolerate_under, convert_resources, admission, should_stop) -> tuple:
    """The map in one Job per part, then the reduce in one Job; ``(ok, message)``.

    The reduce Job is an ordinary campaign-level postprocess with the map steps skipped: it
    stages the tree with every part's outputs in it, runs the campaign-scoped steps, and
    completes the campaign -- index ingest, provenance record, metadata. It stages the
    rosbags only when something left in it opens them.
    """
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        needs_execution_image)

    from .postprocess_parts import (delivered_map_log, run_map_phase,  # noqa: PLC0415
                                     write_plan)

    map_cmds, reduce_cmds, parts = split
    write_plan(campaign_root, parts, force=force, skip=skip)
    ok, message = run_map_phase(
        cluster_config, campaign_id, campaign_root, namespace, image, map_cmds, parts,
        token=token, force=force, kube_context=kube_context, tolerate_under=tolerate_under,
        convert_resources=convert_resources, admission=admission, should_stop=should_stop)
    map_log = delivered_map_log(campaign_root, parts)
    if not ok:
        write_phase_log(campaign_root, map_log + f"{message}\n")
        return record_job_outputs(campaign_id, campaign_root, ok, message,
                                  should_stop=should_stop)
    config_dir = os.path.dirname(campaign_vast(campaign_root))
    reduce_image = [c for c in reduce_cmds if needs_execution_image(c, config_dir)]
    ok, message = run_conversion_job(
        cluster_config, campaign_id, campaign_root, namespace, image, reduce_image,
        token=token, force=force, kube_context=kube_context, tolerate_under=tolerate_under,
        skip=skip, convert_resources=convert_resources, admission=admission,
        role=JobRole.reduce(stage_bags=bool(reduce_cmds)), should_stop=should_stop,
        log_prefix=map_log)
    # The reduce pod delivered its own section, which knows nothing of the parts'.
    _prepend_phase_log(campaign_root, map_log)
    return record_job_outputs(campaign_id, campaign_root, ok, message,
                              should_stop=should_stop)


def _prepend_phase_log(campaign_root: str, prefix: str) -> None:
    if not prefix:
        return
    path = os.path.join(str(campaign_root), *_POSTPROC_LOG_REL.split("/"))
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = ""
    if not current.startswith(prefix):
        write_phase_log(campaign_root, prefix + current)


def record_job_outputs(campaign_id: str, campaign_root: str, ok: bool, message: str,
                       should_stop=None) -> tuple:
    """Turn the Job's verdict into ``(ok, message)`` and the campaign's log.

    Everything a postprocessing Job's outcome means for the campaign tree, in one place, so
    a process that submitted the Job and one that only waited for it leave the campaign in
    the same state. Split from :func:`postprocess_campaign` for that second caller: a
    re-attach has no submit half and must not grow a second account of a failure.

    What the Job produced is already in *campaign_root*: the pod delivered it to the data
    plane, and the pod's log was published there while it ran and once more when it failed
    (:func:`await_job`). What is left to decide is where the message may send the reader.

    *should_stop* is what separates a Job that failed from one this campaign's own stop
    deleted (see :func:`await_job`). The flag latches, so it still answers here; asking it
    rather than matching the message keeps a wording from becoming a contract.
    """

    log_path = os.path.join(str(campaign_root), *_POSTPROC_LOG_REL.split("/"))
    if ok:
        return True, message
    if ok is None:
        # Passed through untouched, and in particular no failure log is authored: that log
        # is the account of a fault, and there is no fault to account for -- the Job may be
        # converting still. The published log is whatever the Job has written so far,
        # which is exactly what a reader wants while the outcome is open.
        logger.warning("Postprocessing outcome unknown: %s", message)
        return None, message
    if should_stop is not None and should_stop():
        # A stop is not a fault either: no failure log is authored and nothing is echoed as
        # one, because the operator asked for this and filing a deliberate act under faults
        # sends whoever reads it looking for a fault that is not there. What the Job wrote
        # before it was deleted is the whole account a cancelled postprocess has -- and
        # the campaign keeps every run result it already produced.
        logger.info("Postprocessing cancelled: %s", message)
        return False, message
    # Echo the error to the service console too. The web UI already has it via the
    # published postprocessing.log (POSTPROCESSING section); no campaign log handler is
    # attached at this point, so this reaches the ``vast serve`` stdout only — not
    # duplicated into the campaign log.
    #
    # This is also what settles WHERE the message may send the reader: a Job whose pod
    # could not be read left no log, so the section it would name does not exist.
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8") as f:
            logger.warning("Postprocessing failed:\n%s", f.read().rstrip())
    else:
        _write_failure_log(campaign_id, log_path, message)
    return False, with_log_pointer(message, log_path)


def run_host_postprocessing(results_dir: str, campaign_id: str, force: bool = False,
                            skip=None, state=None, skip_map: bool = False) -> tuple:
    """Stage 2 — everything that does not run in the execution image (index ingest, metadata).

    Pure Python, so it runs wherever robovast is installed (the controller pod, the
    service pod). Reuses the *normal* pipeline with the execution-image steps skipped — the
    Job's image container already ran those — so there is no second implementation of the
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
    from robovast.results_processing.postprocessing import run_postprocessing

    return run_postprocessing(
        results_dir=results_dir, campaign=campaign_id, force=force,
        skip=sorted(set(skip or ())), skip_image_steps=True, skip_map_steps=skip_map,
        output_callback=stage_output_callback(state, logger.info))


#: Campaign-relative path of the conversion's log. This Job pod runs in a separate context
#: from the service, so its stdout is otherwise only a transient ``kubectl logs``. Teeing
#: the conversion output here puts it in the campaign tree the host container delivers,
#: where it lands at ``<campaign_root>/_execution/postprocessing.log`` — the POSTPROCESSING
#: section of the unified campaign log. The host container appends to the same file, so
#: the two read as one ordered section. While the Job runs, :func:`publish_live_log`
#: writes the pod's own log to the same path.
_POSTPROC_LOG_REL = "_execution/postprocessing.log"

#: Campaign-relative path where the image steps record what they produced from what.
#:
#: The host steps run with the image steps *skipped*, so they have nothing to record for
#: them, and a campaign whose image steps recorded nothing carries a ``postprocessing_steps``
#: table naming only the host's own steps while the others had run. The local lane records
#: them, so without this the provenance a campaign carries would depend on its lane.
_IMAGE_PROVENANCE_REL = "_execution/image_provenance.json"


def _log_rel(part: str = "") -> str:
    return in_part(_POSTPROC_LOG_REL, part, "postprocessing.log")


def _image_provenance_rel(part: str = "") -> str:
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        PART_PROVENANCE_SUFFIX)
    return in_part(_IMAGE_PROVENANCE_REL, part, f"image.{PART_PROVENANCE_SUFFIX}")


def _usage_rel(part: str = "") -> str:
    return in_part(postprocess_usage.USAGE_REL, part, "system_usage.csv")


def campaign_dir(campaign_id: str) -> str:
    """Where the campaign tree sits inside the pod: the stage container's destination.

    One definition, because three containers have to agree on it: the stage container
    extracts the archive under :data:`CAMPAIGN_MOUNT` and its top segment is the campaign
    id, the conversion's arguments are built from it here, and the host container resolves
    the same path from the mount and its ``ROBOVAST_CAMPAIGN_ID``.
    """
    return f"{CAMPAIGN_MOUNT}/{campaign_id}" if campaign_id else CAMPAIGN_MOUNT


#: What the stage container's exit code says, for :func:`pod_failure_reason`.
#:
#: The container is :func:`pod_access.fetch_command`, whose status is ``curl``'s when the
#: transfer failed -- after the retry schedule, unless the service answered 4xx -- and
#: ``tar``'s when a whole stream would not extract. Either way curl prints its report -- the
#: HTTP status, or the address it could not reach -- to the container's log, and the pod's
#: log is published as the campaign's POSTPROCESSING section when the Job fails, so the exit
#: code is the headline and the log is where the reason is read.
STAGE_EXIT_REASONS: dict[int, str] = {
    7: "could not connect to the service's data plane for the whole retry window",
    18: "had the campaign archive cut short on every attempt",
    22: "was refused the campaign archive by the data plane (an HTTP error; the status is "
        "in the POSTPROCESSING section)",
    56: "lost the connection to the data plane on every attempt",
}

#: tar's exit codes. A stream cut short is curl's to report, so tar failing means a whole
#: archive would not extract onto the node.
STAGE_TAR_CODES = (1, 2)
STAGE_TAR_FAILED = ("could not extract the campaign archive onto the node (tar's report is in "
                    "the POSTPROCESSING section; a full disk is the usual cause)")

#: Any other curl failure, named by its code in the headline and explained by curl's report.
STAGE_FETCH_FAILED = ("could not fetch the campaign archive (curl's report is in the "
                      "POSTPROCESSING section)")


def _stage_failure(code: int) -> str:
    """What the stage container's exit *code* says, in the fetch's own vocabulary."""
    if code in STAGE_EXIT_REASONS:
        return STAGE_EXIT_REASONS[code]
    return STAGE_TAR_FAILED if code in STAGE_TAR_CODES else STAGE_FETCH_FAILED


def _stage_query(skip_bags: bool, batch_jobs: str, part: str = "") -> str:
    """The archive route's query for what this pod reads (:class:`ArchiveSelection`).

    ``stage`` always: it drops the calibration probes, the log this pod is about to write
    and the archived log sections. ``uncompressed`` always: the pod is in the cluster, so a
    plain tar is what it extracts at disk speed. ``skip_bags`` when nothing in the pod opens
    a bag, and ``batch_jobs`` for a per-batch Job. Quoted here because
    :func:`pod_access.fetch_command` appends its query verbatim.
    """
    parts = ["stage=true", "uncompressed=true",
             f"skip_bags={'true' if skip_bags else 'false'}"]
    if batch_jobs:
        parts.append(f"batch_jobs={quote(batch_jobs, safe='')}")
    if part:
        parts.append(f"part={quote(part, safe='')}")
    return "&".join(parts)


def _stage_script(campaign_id: str, skip_bags: bool, batch_jobs: str, part: str = "") -> str:
    """The stage initContainer's shell: land the campaign archive, then hand it to the group.

    The transfer is :func:`pod_access.fetch_command`: ``curl | tar`` into the shared mount,
    where the archive's top segment is the campaign id, so the tree lands at
    :func:`campaign_dir`. The ``job`` symlinks and the executable bits of ``_config/``
    arrive as tar members, so nothing has to be restored afterwards.

    What ``tar`` run as root does NOT do is respect the pod's group arrangement: it restores
    each member's owner and mode from the archive, so the tree it leaves is the service's
    user's, with the service's modes, and the conversion container -- another user, in
    :data:`CAMPAIGN_TREE_GID` -- fails on its first output file with EACCES. So the tree is
    given to the group and made group-writable in one pass over it, links excluded: a
    dangling ``job`` link (an interrupted campaign leaves them) is not an error, and a link
    is never the thing whose mode matters.
    """
    from robovast.service.interface import Routes  # noqa: PLC0415

    route = Routes.campaign_archive(campaign_id)[len(Routes.DATA):]
    fetch = pod_access.fetch_command(route, CAMPAIGN_MOUNT,
                                     _stage_query(skip_bags, batch_jobs, part))
    root = _shquote(campaign_dir(campaign_id))
    return (f"{fetch} && find {root} ! -type l -exec chgrp {CAMPAIGN_TREE_GID} {{}} + "
            f"-exec chmod g+rwX {{}} +")


def image_steps_for(campaign_id: str, campaign_root: str, image_cmds: list,
                    force: bool = False, tolerate_under=(), part: str = "") -> list:
    """*image_cmds* as this Job's image container runs them: each plugin's own command,
    for the campaign tree at :func:`campaign_dir` and the provenance file the host reads.

    Plugins resolve against the directory of the campaign's own ``.vast``, where a local
    ``./plugin.py:Class`` reference and its copied files live.
    """
    from robovast.results_processing.postprocessing import image_steps  # noqa: PLC0415
    from robovast.results_processing.postprocessing_plugins import (  # noqa: PLC0415
        ImageContext)

    if not image_cmds:
        return []
    root = campaign_dir(campaign_id)
    ctx = ImageContext(campaign_dir=root,
                       provenance_file=f"{root}/{_image_provenance_rel(part)}",
                       force=force, tolerate_under=tuple(tolerate_under))
    return image_steps(image_cmds, os.path.dirname(campaign_vast(campaign_root)), ctx)


def _conversion_script(steps: list, campaign_id: str = "", part: str = "") -> str:
    """The image container's shell: run each image step, in order, on the campaign tree.

    Reads and writes the shared campaign mount and nothing else. **No token and no
    upload:** this container runs an arbitrary user image (the system under test's), and
    the host container that follows it is what talks to the data plane, so there is
    nothing here for a credential to be needed for.

    Each step's command is its plugin's own
    (:meth:`~robovast.results_processing.postprocessing_plugins.ExecutionImagePlugin.image_command`),
    run through ``ros2_exec.sh`` from the scripts mount -- the same command the local lane
    runs through ``docker_exec.sh``.

    All setup and step stdout/stderr is teed into the campaign's ``postprocessing.log`` so
    it becomes the POSTPROCESSING section of the unified campaign log; the host container
    appends to the same file. ``pipefail`` preserves a step's exit status through the
    ``tee`` pipe.
    """
    from robovast.results_processing.postprocessing_plugins import (  # noqa: PLC0415
        IMAGE_SCRIPTS_DIR)

    from .postprocess_host import IMAGE_STEPS_MARKER  # noqa: PLC0415

    root = campaign_dir(campaign_id)
    log = f"{root}/{_log_rel(part)}"
    convert = []
    for step in steps:
        script, *args = step.argv
        convert.append(" ".join(_shquote(part) for part in (
            f"{IMAGE_SCRIPTS_DIR}/ros2_exec.sh", f"{IMAGE_SCRIPTS_DIR}/{script}", *args)))

    lines = [
        "set -eo pipefail",
        # The log directory is created before anything else, and the setup runs inside the
        # tee, because setup under `set -e` is exactly where the silent failures live: an
        # unwritable campaign tree aborts before the conversion's own output, and the
        # campaign is then pointed at a POSTPROCESSING section that does not exist.
        f"mkdir -p $(dirname {log}) || exit 1",
        # Before the first step: what the host container delivers as this container's
        # output is every file changed after this (postprocess_host._image_step_outputs).
        f"touch {CAMPAIGN_MOUNT}/{IMAGE_STEPS_MARKER} || exit 1",
        "rc=0",
        "(",
        "  set -e",
        "\n".join("  " + c for c in convert),
        f') 2>&1 | tee -a "{log}" || rc=$?',
        # AFTER the tee'd block and outside it, on purpose. What the conversion used is
        # worth recording whether or not it succeeded -- a conversion killed for exceeding
        # its memory is exactly the case the record exists for -- and it must not be able to
        # change `rc`, which is the conversion's own verdict.
        postprocess_usage.shell_record(root, CONVERT_CONTAINER, _usage_rel(part)),
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
    # converts once per repetitions-group; with the name the campaign's alone, the second
    # create returns 409, falls through to the FIRST conversion's completed Job, and
    # reports success having converted nothing.
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
    """Discriminated with its Job: the two are one unit -- see :func:`own_scripts_cm` --
    and a shared name would put two Jobs' scripts on one object, where the second attempt's
    content reaches the first attempt's running interpreter."""
    return _short_job_name(_SCRIPTS_CM_PREFIX, campaign_id, discriminator)


def scripts_configmap_manifest(campaign_id: str, namespace: str,
                               discriminator: str = "", steps=()) -> dict:
    """A ConfigMap carrying the *driver's own* conversion scripts.

    Built from ``robovast.results_processing.data`` — the same package dir the local
    path bind-mounts via ``docker_exec.sh -v <scripts>:/scripts``. Mounting this in the
    conversion Job (instead of copying ``/scripts`` from a separately-versioned
    controller image) makes the in-cluster scripts always match the driver that
    generated the conversion command, so driver/script version skew cannot occur on any
    exec variant. The scripts are self-contained (stdlib + ROS2 libs + one sibling, no
    ``robovast`` import) and small (well under the 1 MiB ConfigMap limit), so a plain
    text ConfigMap suffices.

    The files each of *steps* ships (``image_files``) are added beside them, so a step's
    command finds them where the local lane's ``docker_exec.sh`` mount puts them.
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
    for step in steps:
        for extra in step.files:
            name = os.path.basename(str(extra))
            text = (extra.read_text(encoding="utf-8") if hasattr(extra, "read_text")
                    else open(extra, encoding="utf-8").read())  # pylint: disable=consider-using-with
            if payload.get(name, text) != text:
                raise ValueError(f"{step.name} ships {name!r}, which would replace a "
                                 "different file of the same name in the scripts directory")
            payload[name] = text
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


def own_scripts_cm(core, batch, namespace: str, job_name: str, cm_name: str) -> bool:
    """Make the Job the owner of its scripts ConfigMap. ``True`` if the cluster took it.

    **The ConfigMap's life belongs to the Job that mounts it, not to whoever is waiting
    on that Job.** A waiter stops waiting for reasons that say nothing about the Job --
    its own deadline, a stop request, a service restart, a pod the scheduler has not
    placed yet -- and the Job goes on running without it. Deleting the scripts on the way
    out therefore takes the mount away from a live conversion; the pod that follows cannot
    start at all (``FailedMount``, no such ConfigMap), so the Job stays ``active``
    forever, its campaign stays in ``postprocessing``, and its log ends mid-step with
    nothing said. An ownerReference is what makes that unexpressible: Kubernetes deletes
    the ConfigMap when the Job is deleted and not before, and ``ttlSecondsAfterFinished``
    already deletes every Job that finishes.

    Best-effort, and deliberately so: a cluster that refuses the patch leaves a ConfigMap
    behind (labelled ``jobgroup``/``campaign-id`` for a sweep), which costs a few KiB. The
    alternative failure is the wedged Job above, so this fails towards the leak.

    ``blockOwnerDeletion`` is left off: it requires delete permission on the owner, and
    all this needs is for the ConfigMap to go when the Job does.
    """
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    try:
        job = batch.read_namespaced_job(name=job_name, namespace=namespace)
        core.patch_namespaced_config_map(
            name=cm_name, namespace=namespace,
            body={"metadata": {"ownerReferences": [{
                "apiVersion": "batch/v1", "kind": "Job",
                "name": job_name, "uid": job.metadata.uid,
                "controller": True, "blockOwnerDeletion": False}]}})
        return True
    except (ApiException, AttributeError) as e:
        logger.warning("Scripts ConfigMap %s could not be handed to job %s (%s); it will "
                       "be left behind when the job is reaped", cm_name, job_name, e)
        return False


#: The slot :func:`job_failed_message` leaves for the pointer, filled by
#: :func:`with_log_pointer`. A literal marker rather than string surgery on the message,
#: so that only the ONE message which promises a log gets a pointer: a blocked pod and a
#: timeout carry their own complete explanation and must come through untouched.
POINTER_SLOT = "<<log>>"

#: Appended to a failed Job's message once the pod's log has been published to the
#: campaign. Kept apart from :func:`job_failed_message` because only the caller that holds
#: the campaign's directory knows whether the section it names exists.
LOG_POINTER = ("— see the POSTPROCESSING section of the campaign log for what it "
               "reported")

#: The same slot when no log arrived, and then there is no POSTPROCESSING section and
#: never will be. Pointing at one regardless sends the reader to an empty panel and reads
#: as a second fault on top of the first.
#:
#: The message names the STAGE rather than a cause, because the pod's log is what would
#: have said, and it is exactly what could not be read. Which container it was is in the
#: pod's exit status, which :func:`pod_failure_reason` reads and puts ahead of this.
NO_LOG_POINTER = ("— and its pod's log could not be read, so the campaign log has no "
                  "POSTPROCESSING section and nothing it reported to read. Which step "
                  "failed is in the pod's status: staging the campaign's run data into the "
                  "pod, converting its rosbags, or the host step. Node disk and the "
                  "service's data plane are what to check.")


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

    Where to look is NOT decided here: this runs before the campaign's log has been
    settled, so it cannot know whether a POSTPROCESSING section exists. The caller appends
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
    if POINTER_SLOT not in message:
        return message
    pointer = LOG_POINTER if os.path.isfile(log_path) else NO_LOG_POINTER
    return message.replace(POINTER_SLOT, pointer)


#: :func:`adopt_or_replace` kept a live Job and the caller is now a waiter on someone
#: else's Job -- one it must write nothing into, because that Job is executing out of the
#: scripts it was created with (see :func:`run_conversion_job`).
_JOB_ADOPTED = "adopted"

#: The Job answering to this name was finished and has been deleted and re-created, so the
#: pod that reports back to the caller is the one this attempt asked for.
_JOB_RECREATED = "recreated"


def _stuck_job(core, namespace: str, name: str) -> str:
    """Why this Job's pod will never start, or ``""`` -- including for "it will".

    ``status.active`` counts a pod that cannot start, so without this an attempt meets a Job
    that is permanently Pending, adopts it as work in flight, and waits on an outcome that
    is never coming. A retrigger then does the same, which is what turns one blocked pod
    into a campaign that can no longer be recovered by re-running its postprocessing.

    Only the reasons that will NOT clear on their own: a pod queued behind a busy node or a
    throttled pull is exactly the work in flight adoption exists for, and replacing its Job
    would throw away a conversion that was about to run. Unreadable is not stuck, for the
    reason :func:`live_job` gives.
    """
    from .cluster_execution import blocked_and_contended_reasons  # noqa: PLC0415

    try:
        blocked, contended = blocked_and_contended_reasons(
            core, namespace, f"job-name={name}")
    except Exception as e:  # noqa: BLE001 - advisory only
        logger.debug("Could not check whether %s is stuck: %s", name, e)
        return ""
    return "" if name in contended else blocked.get(name, "")


def live_job(batch, core, namespace: str, name: str) -> bool:
    """Is a Job of this name present, still active, and able to get anywhere?

    Separate from :func:`adopt_or_replace` because the answer is needed *before* anything
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
    if not getattr(getattr(existing, "status", None), "active", None):
        return False
    stuck = _stuck_job(core, namespace, name)
    if stuck:
        logger.info("Postprocessing job %s is active but cannot start (%s); it will be "
                    "replaced rather than waited on", name, stuck)
        return False
    return True


def adopt_or_replace(batch, core, namespace: str, name: str, manifest: dict) -> str:
    """Keep a still-running Job; delete and re-create a finished or stuck one.

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
    stuck = _stuck_job(core, namespace, name) if getattr(status, "active", None) else ""
    if getattr(status, "active", None) and not stuck:
        logger.info("Postprocessing job %s is already running; waiting on it", name)
        return _JOB_ADOPTED

    logger.info("Replacing %s postprocessing job %s",
                f"stuck ({stuck})" if stuck else "finished", name)
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
        time.sleep(POLL_SECONDS)
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
#: lives on a shared volume and is delivered by the last container at the end. So without
#: this a postprocess that takes twenty minutes shows an empty POSTPROCESSING section for
#: twenty minutes, and the only way to watch it is ``kubectl logs`` against a pod name
#: nobody off-cluster has.
#:
#: Each publish reads every container's log from the API server and rewrites the whole
#: file -- which is why this is not every poll. Thirty seconds is slow enough for that to
#: be a rounding error against a conversion measured in minutes, and fast enough to read
#: as progress.
LIVE_LOG_INTERVAL = 30.0

#: Sort key for a pod whose creation time the API did not fill in, so ordering by it never
#: raises. Such a pod loses to any pod that has one, which is the right way round: a
#: timestamp is present on anything the scheduler has seen.
_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def publish_live_log(core, campaign_root, namespace: str, job_name: str,
                     prefix: str = "") -> bool:
    """Write the Job's log to the campaign's POSTPROCESSING phase file. False if not.

    Read from the pod rather than from the volume it writes: the volume is the pod's own and
    nothing outside can see it, while the log is on the pod's stdout by construction -- the
    stage's ``curl`` and ``tar`` report there, the conversion tees there and the host step
    logs there. Written to ``<campaign_root>/_execution/postprocessing.log``, which is the
    file every surface reads the section from; each call replaces the whole file, so the
    section never grows by a copy of itself.

    Every container's output in declaration order, so staging and conversion read as one
    section in the order they ran. A container that has not started yet has no log and is
    skipped, which is also how "this stage has not run" should look.

    *prefix* is written ahead of the Job's own output: what a split postprocess's parts
    logged, which the section keeps while the Job that completes the campaign runs.

    Best-effort throughout: this is a read for someone watching, and it must not fail the
    postprocess it is watching.
    """
    text = read_job_log(core, namespace, job_name)
    if not text:
        return False
    return write_phase_log(campaign_root, prefix + text)


def read_job_log(core, namespace: str, job_name: str) -> str:
    """Every container's output of the Job's newest pod, in declaration order; ``""`` when
    there is none yet. Best-effort: it reads for someone watching."""
    from kubernetes import client  # noqa: PLC0415

    try:
        pods = core.list_namespaced_pod(namespace=namespace,
                                        label_selector=f"job-name={job_name}").items or []
        if not pods:
            return ""
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
        return "".join(chunks)
    except Exception as e:  # noqa: BLE001 - a read for a watcher may not fail the work
        logger.debug("could not read the log of postprocessing job %s: %s", job_name, e)
        return ""


def write_phase_log(campaign_root, text: str) -> bool:
    """Replace the campaign's POSTPROCESSING phase file with *text*. False if not."""
    try:
        log_path = os.path.join(str(campaign_root), *_POSTPROC_LOG_REL.split("/"))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        # Through a sibling and a rename, so a reader streaming the section never sees a
        # half-written file.
        staged = f"{log_path}.live"
        with open(staged, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(staged, log_path)
        return True
    except OSError as e:
        logger.debug("could not publish the live postprocessing log of %s: %s",
                     campaign_root, e)
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
                # The stage container is `curl | tar`, whose codes have a vocabulary of
                # their own; `exited 1 (Error)` names none of them.
                if name == STAGE_CONTAINER:
                    return f"container {name} {_stage_failure(code)} (exit {code})"
                detail = (getattr(term, "reason", None) or "").strip()
                exited = f"container {name} exited {code}"
                return f"{exited} ({detail})" if detail else exited
    return ""


def _blocked_reason(core, namespace: str, job_name: str) -> "tuple[str, bool]":
    """``("<reason>: <message>", contended)`` when this Job's pod cannot start right now,
    else ``("", False)``.

    Reuses the signal the run loop and the image build already act on rather than reading pod
    status again here, so all three agree about what "blocked" means. *contended* is that
    signal's second half: the pod is merely waiting its turn -- for a node that could hold it
    but is busy, or for a throttled pull -- and starts by itself, which decides how long
    :func:`await_job` tolerates it. Advisory: a pod list that cannot be read must not turn a
    running conversion into a reported failure, so an error here yields ``("", False)`` and
    the wait continues to its own deadline.
    """
    from .cluster_execution import blocked_and_contended_reasons  # noqa: PLC0415

    try:
        blocked, contended = blocked_and_contended_reasons(
            core, namespace, f"job-name={job_name}")
    except Exception as e:  # noqa: BLE001 - advisory only
        logger.debug("Could not check whether %s is blocked: %s", job_name, e)
        return "", False
    return blocked.get(job_name, ""), job_name in contended


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


def _git_credentials() -> tuple:
    """The host container's mount of the GitHub token Secret, and the volume behind it.

    The same Secret and path the service pod gets, so ``config_plugins`` finds the token
    where it looks on either side (:data:`~robovast.common.config_plugins.GIT_TOKEN_FILE`).

    **Optional**, because the Secret exists only where the operator gave ``vast cluster
    setup`` a token. A required Secret volume that names nothing holds the pod in
    ``ContainerCreating``; an optional one starts it without the file, and a private
    ``git+https`` plugin then fails its install with the message that names the missing
    token -- the answer the operator needs, where a pod that never starts gives none.
    """
    from .service_deploy import GIT_SECRET_NAME, GIT_TOKEN_MOUNT_DIR  # noqa: PLC0415

    mount = {"name": "git-credentials", "mountPath": GIT_TOKEN_MOUNT_DIR, "readOnly": True}
    volume = {"name": "git-credentials",
              "secret": {"secretName": GIT_SECRET_NAME, "defaultMode": 0o400,
                         "optional": True}}
    return mount, volume


@dataclasses.dataclass(frozen=True)
class JobRole:
    """Which postprocessing Job this is, and therefore what its pod is asked to do.

    One value rather than four flags that only make sense together: a part stages its own
    runs AND runs its host commands AND writes its files under its own name, and the Job
    that completes a split skips the map AND stages the bags only if a step left opens one.
    Built through the four classmethods; there is no fifth kind.
    """

    #: Which conversion of this campaign the Job is, in its name and its ConfigMap's.
    discriminator: str = ""
    #: The part of a split this Job is, if it is one.
    part: str = ""
    #: What the host container runs instead of the campaign-level pass; ``None`` is that pass.
    host_commands: "list | None" = None
    #: Run the campaign-level pass less the steps a split's parts already ran.
    skip_map: bool = False
    #: Whether the stage fetches the rosbags; ``None`` is "exactly when a step opens them".
    stage_bags: "bool | None" = None

    @classmethod
    def campaign(cls) -> "JobRole":
        """The one Job of an unsplit postprocess: every step, then the campaign's completion."""
        return cls()

    @classmethod
    def for_part(cls, name: str, host_commands: list) -> "JobRole":
        """One part of a split: its own runs, its own files, no completion."""
        return cls(discriminator=name, part=name, host_commands=list(host_commands))

    @classmethod
    def reduce(cls, stage_bags: bool) -> "JobRole":
        """The Job that completes a split: what the parts did not run, then the completion."""
        return cls(skip_map=True, stage_bags=stage_bags)

    @classmethod
    def search_batch(cls, tag: str, commands: list) -> "JobRole":
        """A search's per-batch conversion: this batch's own commands, and nothing more."""
        return cls(discriminator=tag, host_commands=list(commands))

    @property
    def batch_jobs(self) -> str:
        """The batch whose job artifacts the stage is narrowed to, or ``""`` for all of them.

        Only a search batch's Job has one: its discriminator is the batch's tag. A part also
        has a discriminator and host commands, but it is narrowed by its runs instead.
        """
        if self.host_commands is None or self.part:
            return ""
        return self.discriminator

    def skips_bags(self, steps) -> bool:
        """Whether the stage leaves the rosbags out: unless a step in this pod opens one."""
        return not (steps if self.stage_bags is None else self.stage_bags)


def build_manifest(campaign_id: str, image, steps: list, namespace: str,
                   role: "JobRole | None" = None, force: bool = False,
                   pull_secret_name: str = "", skip=None,
                   convert_resources=None, stage_bytes=None) -> dict:
    """Build the postprocessing Job manifest.

    Args:
        campaign_id: The campaign to postprocess.
        image: **The campaign's execution image** (the SUT image from
            ``_execution/execution.yaml``) — required for its custom ROS2 types, and
            required *only* for them. Ignored when *steps* is empty: a campaign with no
            step in its execution image needs no image container, so its execution image
            is never pulled and an image that has since gone from the registry does not
            stop it being postprocessed.
        steps: The image steps (:func:`image_steps_for`), run in order in the image
            container. Empty means no image container at all.
        namespace: Kubernetes namespace; also where the service's data plane is addressed
            (:func:`pod_access.data_url`).
        role: Which Job this is (:class:`JobRole`); the campaign-level one by default.
        force: Replace what the host step already wrote. (The image steps carry their own
            ``force`` in their commands.)
        skip: Postprocessing steps the host step must not run.
        convert_resources: ``{"cpu": …, "memory": …}`` the conversion step runs at, from
            :func:`~robovast.results_processing.postprocessing.postprocess_convert_resources`
            -- the campaign's ``results_processing.resources`` over that function's defaults.
            ``None`` takes those defaults, which is what a caller with no ``.vast`` in reach
            must do.
        (A role's ``host_commands`` are the ``search.postprocessing`` commands the host must
            run, for a per-batch Job, or a part's own. ``None`` is the campaign-level Job,
            whose host runs the whole
            ``results_processing.postprocessing`` pass and completes the campaign — index
            ingest, metadata, provenance record. Given, the host runs exactly these and
            completes nothing, because a search reaches this once per batch on a campaign
            that is still growing. The two lists are DIFFERENT blocks of the ``.vast``, so
            a batch Job left to find its own would run the campaign-level one; passing them
            is what keeps the batch running what the controller resolved for it.
            The pod's shape does not change either way; only what the host is asked for does.
        pull_secret_name: Secret for this pod's OWN image pulls -- the sidecar and
            controller images the two robovast containers run, and the campaign's
            execution image. Without it a private-registry deployment sits in
            ``ImagePullBackOff`` while the Job stays ``active``, so the wait reports a
            timeout naming neither the image nor the registry.
        stage_bytes: What the stage will extract (:func:`stage_bytes`), which sizes its
            disk request; ``None`` leaves the floor.

    The pod reaches the data plane with :func:`pod_access.campaign_pod_env`: the address,
    the campaign id and the campaign's token from its Secret -- which
    :func:`run_conversion_job` creates before the Job, because a pod whose ``secretKeyRef``
    names nothing waits in ``CreateContainerConfigError``.

    The ``/scripts`` come from a per-campaign ConfigMap (see
    :func:`scripts_configmap_manifest`) built from the driver's own
    ``results_processing/data`` — the K8s analog of ``docker_exec.sh``'s
    ``-v <scripts>:/scripts`` — so the script version always matches the driver. It is
    mounted, and the ConfigMap volume declared, only where a conversion container exists.
    """
    from .postprocess_host import (ENV_COMMANDS, ENV_FORCE,  # noqa: PLC0415
                                   ENV_PART, ENV_SKIP, ENV_SKIP_MAP, ENV_STAGE_DEST)

    role = role or JobRole.campaign()
    discriminator, part = role.discriminator, role.part
    batch_commands, skip_map = role.host_commands, role.skip_map

    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        POSTPROCESS_CONVERT_DEFAULTS)

    sized = convert_resources or POSTPROCESS_CONVERT_DEFAULTS
    convert_resources_block = step_resources(sized["cpu"], sized["memory"])
    host_block = step_resources(**raised_to(POSTPROCESS_HOST_FLOOR, sized))

    # The two containers that run Python and whose output the campaign log is read from
    # (see publish_live_log): stdout here is a pipe rather than a terminal, and Python
    # block-buffers a pipe, so without this a step's output reaches the log in ~8 KB clumps
    # long after it happened -- and the reason to publish a running postprocess at all is
    # that someone is watching it. Logging handlers flush per record and are unaffected;
    # what this recovers is `print` and the conversion's own progress output.
    unbuffered_env = [{"name": "PYTHONUNBUFFERED", "value": "1"}]
    # The two robovast containers reach the data plane; the conversion does not, and gets
    # none of this. See the conversion container below.
    data_plane_env = pod_access.campaign_pod_env(namespace, campaign_id)
    campaign_mount = {"name": "campaign", "mountPath": CAMPAIGN_MOUNT}
    git_mount, git_volume = _git_credentials()

    stage = {
        "name": STAGE_CONTAINER,
        # The sidecar image: `curl` and `tar` are the whole of what staging needs, and a
        # small image is one the pod waits less for.
        "image": resolve_sidecar_image(),
        "command": ["sh", "-c", _stage_script(
            campaign_id,
            # Bags are staged only where something in this pod opens one. The host step
            # never does -- it reads the derived tables and the run metadata -- so a
            # campaign with no conversion container stages the campaign tree WITHOUT its
            # rosbags, which is the bulk of a campaign by orders of magnitude. Staging them
            # anyway would spend the whole download and the whole node disk on data
            # nothing in the pod reads.
            skip_bags=role.skips_bags(steps),
            # One batch's job artifacts, for a per-batch Job. The bags are the bulk of a
            # campaign and every batch's sit under the same tree, so without this a search
            # stages every earlier batch again on every batch.
            batch_jobs=role.batch_jobs,
            # A part of a split postprocess stages its own runs and their jobs.
            part=part)],
        "env": data_plane_env,
        "volumeMounts": [campaign_mount],
        "resources": stage_resources(stage_bytes),
    }
    convert = {
        "name": CONVERT_CONTAINER,
        # The system-under-test's own image: custom ROS2 types only deserialize here.
        # ros2_exec.sh sources /opt/ros + /ws/install.
        "image": image,
        "command": ["/bin/bash", "-c",
                    _conversion_script(steps, campaign_id=campaign_id, part=part)],
        # **No token, deliberately.** This container reads and writes the shared campaign
        # mount and nothing else, and it is an arbitrary user image -- the campaign's own
        # -- so it is the one container in this pod that must hold nothing that would let
        # it reach the data plane or the index. Buffering is not a credential: this is
        # where the conversion's progress output comes from, and it is the longest step,
        # so it is the one whose output most needs to arrive while it runs.
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
        # `umask 0002` for a reason that outlives this pod: what this step derives is
        # delivered as the campaign's own, so a re-run staging it again must be able to
        # write over it, and the conversion's own outputs live in the same tree.
        "command": ["sh", "-c",
                    "umask 0002 && exec python3 -m "
                    "robovast.execution.cluster_execution.postprocess_host"],
        # The index DSN is injected HERE and nowhere else: this is the only container that
        # writes to the index.
        "env": unbuffered_env + data_plane_env + _index_env(namespace) + [
            {"name": ENV_STAGE_DEST, "value": CAMPAIGN_MOUNT},
            {"name": ENV_FORCE, "value": "1" if force else "0"},
            {"name": ENV_SKIP, "value": ",".join(sorted(set(skip or ())))},
            *([{"name": ENV_COMMANDS, "value": json.dumps(batch_commands)}]
              if batch_commands is not None else []),
            *([{"name": ENV_PART, "value": part}] if part else []),
            *([{"name": ENV_SKIP_MAP, "value": "1"}] if skip_map else []),
        ],
        # The GitHub token, for the one thing here that needs it: the host re-installs the
        # campaign's `plugins:` before its steps run, and a `git+https` spec for a private
        # repository cannot be cloned without it. Mounted as a file, in this container
        # only -- the conversion runs the campaign's own image and gets nothing.
        "volumeMounts": [campaign_mount, git_mount],
        "resources": host_block,
    }

    init_containers = [stage]
    if steps:
        init_containers.append(convert)
    # initContainers run sequentially to completion in declaration order and the main
    # containers start only after they all succeed. That ordering IS the orchestration
    # here; there is no code sequencing these steps.
    #
    # The host container runs in BOTH shapes, and the shape decides only what it is asked
    # for. It is the only container that delivers, deliberately -- the conversion runs the
    # campaign's own image -- so a Job that ended at the conversion could not send its
    # outputs anywhere, and they would go with the pod's emptyDir.
    containers = [host]

    volumes = [
        # One copy of the campaign, shared by every container.
        {"name": "campaign", "emptyDir": {}},
        git_volume,
    ]
    if steps:
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
    return campaign_job_manifest(
        name=_short_job_name("robovast-postproc-", campaign_id, discriminator),
        namespace=namespace, jobgroup=POSTPROCESS_JOBGROUP, campaign_id=campaign_id,
        ttl_seconds=300, pull_secret=pull_secret_name,
        pod_spec={
            # One tree, written by containers that run as different users -- see
            # CAMPAIGN_TREE_GID. supplementalGroups covers an execution image whose own
            # user is not the family's.
            "securityContext": {"fsGroup": CAMPAIGN_TREE_GID,
                                "supplementalGroups": [CAMPAIGN_TREE_GID]},
            "volumes": volumes,
            "initContainers": init_containers,
            "containers": containers,
        })


def cancel_job(batch, namespace: str, name: str) -> str:
    """Delete a postprocessing Job whose campaign was stopped; return the stated reason.

    Deleted rather than left to finish, because the point of a stop is that its compute
    ends now, and this Job is not reached by the teardown a stop already performs: that one
    is scoped to ``jobgroup=scenario-runs`` so that it cannot cancel a content-addressed
    image build a sibling campaign may be waiting on.

    ``grace_period_seconds=0`` with foreground propagation, the same terms that teardown
    uses: the conversion has no shutdown work worth waiting for, and a bag interrupted
    mid-write costs nothing -- a bag records itself as converted only once its handlers
    have finished, so the next run simply redoes it.

    A deletion that fails is reported as the cancellation it was anyway, and any failure
    rather than an API refusal alone: a stop can be a Ctrl+C that takes the route to the
    cluster with it, and raising then would replace the operator's own stop with a
    traceback from tidying up after it. The Job's ``ttlSecondsAfterFinished`` collects it.
    """
    from kubernetes import client  # noqa: PLC0415
    try:
        batch.delete_namespaced_job(
            name=name, namespace=namespace,
            body=client.V1DeleteOptions(grace_period_seconds=0,
                                        propagation_policy="Foreground"))
        logger.info("Postprocessing job %s deleted: the campaign was stopped", name)
    except Exception as e:  # noqa: BLE001 - see above
        logger.warning("Could not delete postprocessing job %s: %s", name, e)
    return ("postprocessing cancelled: the campaign was stopped. The runs and their "
            "results are untouched; re-run postprocessing to derive the data.")


def await_job(core, batch, campaign_root, namespace: str, name: str,
              timeout: int = DEFAULT_TIMEOUT,
              batch_commands=None, should_stop=None, log_prefix: str = "") -> tuple:
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

    *campaign_root* is where the pod's log is published while it runs
    (:func:`publish_live_log`), and once more when the Job is read as failed: a failure in
    an initContainer delivers nothing, and the pod's stdout -- ``curl``'s report, the
    conversion's tee -- is then the only account there is, readable for the 300 s the Job
    is kept.

    *batch_commands* names what the Job was asked to do, and so what its success means: a
    batch Job has derived one batch's tables and completed no campaign.

    *should_stop* is polled here rather than by either caller, which is what makes a stop
    reach a Job this process merely found as well as one it submitted: a campaign stopped
    during postprocessing would otherwise wait out a Job that can run for hours, while its
    stop reported itself as done. A stop deletes the Job and is read as ``False`` -- the
    outcome is not unknown, since nothing is going to produce it now.
    """
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    from .cluster_execution import (BLOCKED_GRACE_SECONDS,  # noqa: PLC0415
                                    CONTENDED_GRACE_SECONDS)

    deadline = time.time() + timeout
    # Published from here because this is the only place that knows the Job is still
    # running. Nothing the pod writes leaves it until it exits, so without this the
    # POSTPROCESSING section stays empty for the whole of a conversion measured in
    # minutes -- and the only way to watch one is a pod name nobody off-cluster has.
    next_live_log = 0.0
    blocked_since = None  # when the pod was first seen unable to start, while it still is
    while time.time() < deadline:
        if should_stop is not None and should_stop():
            return False, cancel_job(batch, namespace, name)
        if time.time() >= next_live_log:
            publish_live_log(core, campaign_root, namespace, name, prefix=log_prefix)
            next_live_log = time.time() + LIVE_LOG_INTERVAL
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
                # the pod that knows why is gone. The log likewise: a pod that failed
                # before its host step delivered nothing, and its stdout is the account.
                publish_live_log(core, campaign_root, namespace, name, prefix=log_prefix)
                return False, job_failed_message(
                    name, pod_reason=pod_failure_reason(core, namespace, name))
        # A pod that CANNOT start leaves the Job `active` forever, so the polling above
        # never sees a verdict and this returns "timed out" -- naming a duration where the
        # cause was an unpullable image or an unschedulable pod. The same signal the run
        # loop and the image build already act on, with the same two tolerances: a pod
        # waiting its turn for a busy node or a throttled pull starts by itself, so it gets
        # the long one; anything else looks the same in ten minutes as in one.
        #
        # Never on first sight. The queue reserved this pod's room before creating it, but
        # Kubernetes charges a deleted pod's requests to its node until the pod is gone, and
        # the campaign's last run pods are still terminating when this one is created. On a
        # node with room for one or the other, the scheduler's first pass finds it full and
        # marks the pod Unschedulable; its next pass places it. Read as a verdict, that
        # first pass is a campaign whose postprocessing "failed" without ever running.
        blocked, contended = _blocked_reason(core, namespace, name)
        if not blocked:
            blocked_since = None
        else:
            if blocked_since is None:
                blocked_since = time.time()
            grace = CONTENDED_GRACE_SECONDS if contended else BLOCKED_GRACE_SECONDS
            if time.time() - blocked_since >= grace:
                return False, (
                    f"postprocessing job {name} cannot start, and has not for {grace:g}s: "
                    f"{blocked}. This is about the pod -- pulling the sidecar or "
                    f"controller image or the campaign's own execution image, finding a "
                    f"node for it, or mounting what it needs -- not about postprocessing, "
                    f"which has not run. Nothing about the campaign's results is wrong; "
                    f"re-run postprocessing once the pod can start.")
        sleep_unless_stopped(POLL_SECONDS, should_stop)
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
    """``{label-safe campaign: [job name, ...]}`` for every postprocessing Job still active.

    One labelled listing, not a read per campaign: the Job carries
    ``jobgroup=postprocessing`` and its campaign's label-safe id (see
    :func:`build_manifest`), so what is running can be asked of the cluster rather than
    guessed from the set of campaigns anyone happens to know about.

    The label is the *sanitized* id and several ids can sanitize to one label, so the
    caller resolves it against the campaigns it knows and confirms the name with
    :func:`campaign_job_name` or the part names of a split. Returns empty when the
    cluster cannot be listed: a Job that cannot be read is not a Job whose outcome anyone
    may record.
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
            live.setdefault(campaign, []).append(job.metadata.name)
    return live


def reattach_conversion_job(campaign_id: str, campaign_root: str, namespace: str,
                            job_name: str, timeout: int = DEFAULT_TIMEOUT,
                            kube_context=None, should_stop=None) -> tuple:
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
            if not live_job(batch, core, namespace, job_name):
                return None, (f"postprocessing job {job_name} is no longer active, so this "
                              f"process has no outcome to record for {campaign_id}")
    except ClusterUnreachableError as e:
        return None, f"the postprocessing job {job_name} could not be read: {e}"
    logger.info("Re-attached to the postprocessing job %s already in flight; its scripts "
                "are untouched", job_name)
    ok, message = await_job(core, batch, campaign_root, namespace, job_name,
                            timeout=timeout, should_stop=should_stop)
    return record_job_outputs(campaign_id, campaign_root, ok, message,
                              should_stop=should_stop)


class JobSubmitFailed(RuntimeError):
    """A postprocessing Job could not be created. Its message is the caller's to report."""


def submit_postprocess_job(core, batch, namespace: str, campaign_id: str, name: str,
                           manifest: dict, steps: list, discriminator: str = "",
                           token: str = "") -> None:
    """Create one postprocessing Job with the scripts it mounts. Raises on failure.

    The one place a postprocessing Job is created, for the campaign-level Job, a search's
    per-batch conversion and every part of a split -- because what is delicate here is the
    same for all of them:

    * the campaign's **token Secret** exists before the Job, whose ``secretKeyRef`` names
      it; a pod whose Secret is missing waits in ``CreateContainerConfigError``;
    * the **scripts ConfigMap** is written before the Job, because the pod waits in
      ``ContainerCreating`` until its volume source exists, and only where a container
      mounts it;
    * a **409** means a Job of this name is already there. A finished one is replaced -- its
      outcome is an earlier attempt's -- and one that raced us is adopted
      (:func:`adopt_or_replace`);
    * the ConfigMap becomes the **Job's** (:func:`own_scripts_cm`) the moment a Job exists,
      and is deleted only when no Job ever mounted it. A waiter that deletes the scripts of
      a running Job wedges it, and rewriting them swaps the script out from under the
      interpreter -- which is why an adopted Job's scripts are never touched, and this is
      called only where this attempt creates the Job.

    Raises :class:`JobSubmitFailed`, so a caller that reports an outcome turns it into one
    and the admission queue's create callback simply lets it out (the queue retries, and
    records the reason as the owner's refusal).
    """
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    from robovast.common.errors import ClusterUnreachableError  # noqa: PLC0415

    owned_cm_name = ""
    try:
        if token:
            try:
                with api_transport_errors("submitting the postprocessing job"):
                    pod_access.ensure_campaign_secret(core, namespace, campaign_id, token)
            except ApiException as e:
                raise JobSubmitFailed(
                    f"could not write the campaign's data-plane token Secret: {e}") from e
        if steps:
            cm = scripts_configmap_manifest(campaign_id, namespace,
                                            discriminator=discriminator, steps=steps)
            cm_name = cm["metadata"]["name"]
            try:
                with api_transport_errors("submitting the postprocessing job"):
                    try:
                        core.create_namespaced_config_map(namespace=namespace, body=cm)
                    except ApiException as e:
                        if e.status != 409:
                            raise JobSubmitFailed(
                                f"could not create postprocessing scripts ConfigMap: {e}") from e
                        # A stale copy from a prior run, mounted by no live Job: replaced.
                        core.replace_namespaced_config_map(name=cm_name, namespace=namespace,
                                                           body=cm)
            except ApiException as e:
                raise JobSubmitFailed(
                    f"could not create postprocessing scripts ConfigMap: {e}") from e
            owned_cm_name = cm_name
        try:
            with api_transport_errors("submitting the postprocessing job"):
                batch.create_namespaced_job(namespace=namespace, body=manifest)
        except ApiException as e:
            if e.status != 409:
                raise JobSubmitFailed(f"could not create postprocessing job: {e}") from e
            if not adopt_or_replace(batch, core, namespace, name, manifest):
                raise JobSubmitFailed(
                    f"postprocessing job {name} already exists and could not be replaced; "
                    f"retry once it has been removed") from e
        if owned_cm_name:
            own_scripts_cm(core, batch, namespace, name, owned_cm_name)
            owned_cm_name = ""
    except ClusterUnreachableError as e:
        raise JobSubmitFailed(f"postprocessing cannot be scheduled: {e}") from e
    finally:
        # Only a ConfigMap no Job ever mounted: the create failed, or the cluster went away
        # between the two writes.
        if owned_cm_name:
            try:
                core.delete_namespaced_config_map(name=owned_cm_name, namespace=namespace)
            except ApiException as e:
                if e.status != 404:
                    logger.warning("Could not delete scripts ConfigMap %s: %s",
                                   owned_cm_name, e)


def run_conversion_job(cluster_config, campaign_id: str, campaign_root: str,
                       namespace: str, image, image_cmds: list, *, token: str,
                       force: bool = False,
                       timeout: int = DEFAULT_TIMEOUT, kube_context=None,
                       role: "JobRole | None" = None, tolerate_under=(), skip=None,
                       convert_resources=None, admission=None, should_stop=None,
                       log_prefix: str = "") -> tuple:
    """Create the postprocessing Job and wait for it. Returns ``(ok, message)``.

    ``ok`` is three-valued, and the third value is the point of it: ``True`` the Job was
    read as succeeded, ``False`` it was read as failed, ``None`` **this process can no
    longer see what the Job is doing** -- the API server would not answer, or the wait
    reached its deadline while the Job was still active. ``None`` is not a synonym for
    ``False``: the Job runs in the cluster and outlives this process, so a driver that
    loses sight of it has learned nothing about the conversion. Recorded as a failure it
    sends someone to redo hours of work over a conversion that finished, and marks a
    campaign whose derived data is complete as carrying none.

    *campaign_root* is the campaign's directory on the service's results volume: what the
    stage's disk request is sized from, and where the pod's log is published while it runs.
    *token* is the campaign's scoped data-plane token; it is put in the campaign's Secret
    (:func:`pod_access.ensure_campaign_secret`) before the Job, which is what the pod's
    ``secretKeyRef`` resolves. *cluster_config* is read for one thing, the pull Secret the
    pod's own images need (:func:`~.cluster_execution.resolve_pull_secret`); nothing about
    where the campaign's bytes are comes from it.

    *image_cmds* are the postprocessing entries that run in the execution image
    (:func:`image_commands_for`), in order; each is turned into its command here, for this
    Job's paths, by its own plugin. *tolerate_under* names the jobs whose output was cut
    short (:func:`~robovast.results_processing.postprocessing_plugins._interrupted_job_dirs`),
    which the image steps report instead of failing on.

    *image* is the campaign's execution image, and is needed only for the image steps: an
    empty *image_cmds* builds a Job that never pulls it. Callers that cannot know in
    advance whether it is needed should pass ``None`` and let *image_cmds* decide, so a
    campaign whose image has gone from the registry still postprocesses.

    *role* says which Job this is (:class:`JobRole`): the campaign-level one by default, a
    search's per-batch conversion, or the Job that completes a split postprocess. With a
    role that names commands, an empty list and nothing to convert is no work at all, which
    is a no-op success.

    *admission* is the deployment's queue. Given, this pod waits for room like every other
    pod on the cluster rather than being created against a cluster that has none -- see
    :func:`await_admission`. ``None`` creates it directly, which is what a lane with no queue
    (a local service, an off-cluster driver) must do.

    *should_stop* ends the wait early for a campaign that was stopped, deleting the Job --
    see :func:`await_job`, which polls it.

    *log_prefix* is written ahead of this Job's own output in the campaign log: what a
    split postprocess's parts logged, which the Job completing it keeps.

    A role's *discriminator* names WHICH conversion of this campaign this is, and must be
    set by any caller that converts the same campaign more than once -- a search, which
    converts once per repetitions-group, and a split, which converts once per part. Without
    it the second create returns 409 and the wait below reads the FIRST conversion's
    already-completed Job, returning "rosbag conversion complete" having converted nothing.
    The campaign-level role has none, where the 409 fallthrough is right: a retry of a
    single conversion should wait on the Job already in flight rather than launch a second.
    """
    role = role or JobRole.campaign()
    discriminator, batch_commands = role.discriminator, role.host_commands
    if not image_cmds and batch_commands is not None and not batch_commands:
        return True, "no postprocessing step configured; nothing to run"
    if image_cmds and not image:
        # Refused rather than defaulted: converting in the wrong image deserializes the
        # campaign's custom message types against a stranger's definitions, and what comes
        # out of that is wrong data rather than an error.
        return False, ("no execution image for the campaign's image steps; its custom ROS2 "
                       "types deserialize in no other image")
    # Before anything is submitted: a step that cannot say what to run in the image is a
    # configuration fault, and finding it here costs nothing.
    try:
        steps = image_steps_for(campaign_id, campaign_root, image_cmds, force=force,
                                tolerate_under=tolerate_under)
    except (KeyError, ValueError, ImportError, FileNotFoundError, AttributeError) as e:
        return False, f"postprocessing cannot run its execution-image steps: {e}"
    if not token:
        # The pod can reach the data plane with nothing else, and a Job submitted without
        # it would stage nothing and then sit in CreateContainerConfigError on a Secret
        # that was never written.
        raise ValueError(f"no data-plane token for campaign {campaign_id}; the "
                         "postprocessing pod could reach neither the campaign archive "
                         "nor the outputs route")

    from kubernetes import client  # noqa: PLC0415

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
        campaign_id, image, steps, namespace, force=force,
        pull_secret_name=resolve_pull_secret(cluster_config, core, namespace),
        role=role, skip=skip, convert_resources=convert_resources,
        stage_bytes=stage_bytes(campaign_root, skip_bags=role.skips_bags(steps),
                                batch_jobs=role.batch_jobs, part=role.part))
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
    # cluster is back -- the runs themselves are already on the service -- rather than
    # reaching the caller as a urllib3 traceback.
    try:
        with api_transport_errors("submitting the postprocessing job"):
            adopted = live_job(batch, core, namespace, name)
    except ClusterUnreachableError as e:
        return False, f"postprocessing cannot be scheduled: {e}"

    # Queued for capacity BEFORE anything is written, and only when this attempt is the one
    # creating the Job: an adopted Job already holds real capacity on a real node, so
    # admitting it a second time would charge the cluster twice for one pod.
    admitted = False
    if admission is not None and not adopted:
        granted, node_id, message = await_admission(admission, campaign_id, name, manifest,
                                                    timeout=timeout, should_stop=should_stop)
        if not granted:
            return False, message
        admitted = True
        # The job node pool and the granted node only -- never the campaign's
        # `execution.kubernetes.jobs.node`, deliberately. Postprocessing has no calibration to
        # stay comparable with, it is the largest single pod a campaign asks for, and on its
        # campaign's node it would queue behind that campaign's own trials.
        pin_campaign_job(manifest, node_id)

    # The campaign's token Secret, before the Job: the pod's `secretKeyRef` names it, and a
    # pod whose Secret does not exist waits in CreateContainerConfigError with the Job
    # `active`. Idempotent, so an attempt meeting a Secret the campaign's runs already
    # created writes nothing. Only where this attempt creates the Job -- an adopted one is
    # running, so its Secret exists.
    #
    # The conversion scripts arrive as a per-campaign ConfigMap mounted at /scripts —
    # the driver's own copy, so no controller-image version skew. Create it before the
    # Job, because the pod waits in ContainerCreating until the volume source exists. Only
    # where something mounts it: a Job with no conversion container declares no such volume,
    # and creating the ConfigMap anyway would leave one behind for every host-only
    # postprocess.
    #
    # `owned_cm_name` is a ConfigMap this attempt created that NOTHING yet mounts, and it is
    # the only thing the cleanup below deletes. It is cleared the moment a Job of ours
    # exists, because from then on the Job's life decides the ConfigMap's -- see
    # :func:`own_scripts_cm`, and :data:`_JOB_ADOPTED` for the Job that was never ours.
    if adopted:
        logger.info("Waiting on the postprocessing job %s already in flight; its "
                    "scripts are untouched", name)
    else:
        try:
            submit_postprocess_job(core, batch, namespace, campaign_id, name, manifest,
                                   steps, discriminator=discriminator, token=token)
        except JobSubmitFailed as e:
            if admitted:
                admission.finished(name)
            return False, str(e)
        logger.info("Postprocessing job %s created (conversion image=%s)", name,
                    image if steps else "none needed")

    try:
        return await_job(core, batch, campaign_root, namespace, name,
                         timeout=timeout, batch_commands=batch_commands,
                         should_stop=should_stop, log_prefix=log_prefix)
    finally:
        # Release the reservation the moment the pod is gone, so what it held is spendable on
        # the next drain. In the `finally` because every exit from here -- finished, failed
        # or timed out -- ends the pod's claim, and a reservation left behind would shrink
        # the cluster by a pod that no longer exists for as long as this service runs.
        if admitted:
            admission.finished(name)
