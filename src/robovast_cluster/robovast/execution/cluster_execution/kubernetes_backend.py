#!/usr/bin/env python3
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
"""Kubernetes execution backend for the in-cluster campaign controller.

:class:`KubernetesBackend` is the cluster counterpart of
:class:`~robovast.execution.backends.DockerBackend`: it runs **one batch** of a
campaign as Kubernetes Jobs and leaves results at
``<campaign_root>/<config>/<run>/`` so the controller's scoring and store are
backend-agnostic. It is meant to run **inside the controller pod**
(a cluster campaign), where the storage backend is reachable directly and
with full bandwidth.

The job-manifest toolkit lives here in :class:`BatchJobRunner` (built only via
:meth:`BatchJobRunner.for_batch`): it composes per-job Kubernetes manifests,
submits/polls/cleans up the Jobs, and writes the per-run job-link manifest.

Per batch it:

1. prepares the batch's config tree (reusing :func:`prepare_campaign_configs`
   and the :class:`BatchJobRunner` manifest building),
2. uploads it to the campaign's storage prefix (in-pod, via
   :mod:`.in_pod_storage` — no ``kubectl``/archiver),
3. creates one Kubernetes Job per packed job and waits for completion, then
4. downloads that batch's per-config/run results, its job-artifact dir
   (``_jobs/<batch_tag>/`` — ``sysinfo.yaml``, resource monitor, logs) **and** the
   campaign-level snapshot (``_config``/``_transient``) back into ``campaign_root``,
   materialises the per-run ``job`` symlinks, and records
   ``_execution/execution.yaml``.

Steps 1–4 leave ``campaign_root`` **complete** — the same shape a local
(``DockerBackend``) run leaves — so the backend-agnostic analysis postprocessing
and the canonical publish (:meth:`KubernetesBackend.finalize_campaign`) consume it
identically. The object store is the source of truth; ``campaign_root`` is its
projection (the same one the service's ``fetch_campaign`` reconstructs on re-run).

Each batch is isolated under a ``_batches/<batch_tag>/`` storage sub-prefix and
uses batch-namespaced job names, so batches of one search campaign never
collide. A ``_batch_tag`` of ``None`` selects the classic single-batch layout.
"""

import copy
import hashlib
import logging
import os
import re
import shlex
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from kubernetes import client

from robovast.common import (COMPAT_VERSION, MIN_IMAGE_COMPAT, get_execution_env_variables,
                             plan_containers, prepare_campaign_configs, scenario_env)
from robovast.common.campaign_data import (KIND_INVALID, record_container_failures,
                                           record_intervention)
from robovast.common.common import get_scenario_parameters
from robovast.common.config import SCENARIO_CONTAINER, job_deadline_seconds
from robovast.common.execution import (build_job_parameter_documents, create_job_links,
                                       dump_multi_document_yaml, job_artifact_rel, node_label,
                                       read_job_links, resolve_sidecar_image,
                                       sidecar_backend_env, write_job_links_manifest)
from robovast.common.simulators import SIM_OVERRIDES_MOUNT, SIMULATION_CONTAINER, sim_job_overlay
from robovast.execution.backends import (CampaignConfigError, CampaignStopped, ExecutionBackend,
                                         RunOptions)
from robovast.execution.packer import build_jobs

from . import in_pod_storage
from .cluster_context import resolve_resources
from .cluster_execution import (BLOCKED_GRACE_SECONDS, CONTENDED_GRACE_SECONDS,
                                _label_safe_campaign, blocked_and_contended_reasons,
                                previous_container_log, restarted_job_forensics)
from .kubernetes_gpu import GPU_RESOURCE
from .manifests import CALIBRATION_JOB_KIND, JOB_KIND_LABEL, JOB_TEMPLATE, MAIN_CONTAINER_NAME
# Re-exported so the poll loop reads as prose: it consults these every two seconds, and an
# import inside the loop would be noise. node_admission imports nothing from this package,
# so there is no cycle to route around by importing late.
from .node_admission import CREATED as _ADMIT_CREATED
from .node_admission import PLANNED as _ADMIT_PLANNED
from .node_placement import NODE_ID_LABEL, job_node_pool

logger = logging.getLogger(__name__)

#: Handed to the NVIDIA container runtime for a GPU container. The load-bearing member is
#: ``graphics``: it is what injects ``/dev/dri``, ``libEGL_nvidia`` and the glvnd ICD, and
#: without it the container gets the device but no way to render on it -- which is not an
#: error anywhere, just a job that quietly renders in software many times slower. ``all``
#: rather than an explicit list so one ``.vast`` key means the same thing here as on the
#: Compose lane, which already writes exactly this.
GPU_DRIVER_CAPABILITIES = "all"

# How often (seconds) the result-download progress logger emits a running count.
_DOWNLOAD_PROGRESS_INTERVAL = 5.0


def _download_progress_logger(batch_tag, interval=_DOWNLOAD_PROGRESS_INTERVAL):
    """Return a no-argument callback that logs the running download count.

    Passed to ``StorageClient.download_prefix`` as ``on_file`` — it is called
    once per fetched file and emits a throttled ``downloaded N so far`` line so a
    large batch's projection shows progress instead of sitting silent. The count
    is cumulative across the several ``download_prefix`` calls a search-mode batch
    makes (the callback is shared), so the log reads as one continuous total.
    """
    state = {"count": 0, "last": time.monotonic()}

    def on_file():
        state["count"] += 1
        now = time.monotonic()
        if now - state["last"] >= interval:
            state["last"] = now
            logger.info("Batch %s: downloaded %d result file(s) so far...",
                        batch_tag, state["count"])

    return on_file


def pull_policy_for(image_ref: str) -> str:
    """The ``imagePullPolicy`` a container running *image_ref* must carry.

    ``IfNotPresent`` for a digest-pinned ref, ``Always`` for anything else, and **always
    written out** -- never left to Kubernetes' default, which is the trap this exists to
    close. That default is ``IfNotPresent`` *except* for a ``:latest`` tag, where it
    silently becomes ``Always``; the campaign image is a floating ``:latest`` in the
    ordinary case, so every container of every scenario pod re-contacted the registry on
    every start even though the node already had the image.

    A batch of thirty-five pods is then ~140 registry round trips delivered in one
    instant, against a kubelet whose image-pull limiter is five per second
    (``registryPullQPS``, burst ten). The pods past the burst come back
    ``ErrImagePull: pull QPS exceeded`` -- not a blip but arithmetic, on every batch.

    The policy follows the ref rather than being chosen: a digest names the bytes, so
    "if not present" cannot serve anything stale, while a tag can be re-pushed under us
    and must be re-checked. That is why pinning (:meth:`BatchJobRunner._pin_image_refs`)
    is the half that does the work -- this half only stops the answer depending on
    whether the tag happens to read ``latest``.
    """
    return "IfNotPresent" if "@sha256:" in (image_ref or "") else "Always"


def _instance_type_command(cluster_config) -> str | None:
    """The provider's shell line for recording the node's instance type, or ``None``.

    Each cluster config implements ``get_instance_type_command`` — GCP and Azure query
    their metadata service for the machine type, bare-metal providers report the
    architecture. Best-effort: a provider that does not implement it (the base class
    raises) simply records no instance type, because sysinfo collection must never be the
    reason a campaign fails.

    .. todo::

       Only the ``uname -m`` implementations (rke2, minikube) have been exercised. The
       GCP and Azure commands query a cloud metadata service and are **untested** —
       verify the recorded ``instance_type`` on a real run on each before relying on it
       (a wrong URL or response shape would silently record an empty string).
    """
    try:
        return cluster_config.get_instance_type_command()
    except (NotImplementedError, AttributeError):
        logger.debug("cluster config %s records no instance type",
                     type(cluster_config).__name__)
        return None


def _s3_env(endpoint, bucket, access_key, secret_key, prefix) -> tuple:
    """The credentials ``/tmp/s3_upload.sh`` reads at run time.

    Given to the sidecars as well as the main container. A sidecar runs the same script
    when its workload exits, because whatever it wrote after the main container's upload
    -- the simulator's recording, and the tail of every sidecar log -- would otherwise die
    with the pod's emptyDir.
    """
    return (
        ('S3_ENDPOINT', endpoint),
        ('S3_BUCKET', bucket),
        ('S3_ACCESS_KEY', access_key),
        ('S3_SECRET_KEY', secret_key),
        ('S3_PREFIX', prefix),
    )


def _run_output_dir_env(job) -> tuple:
    """``RUN_OUTPUT_DIR`` for a job that is exactly one run, else nothing.

    ``/out`` is the pod's campaign root and ``OUTPUT_DIR`` is a per-*job* subdir, so neither names the
    place this run's results land -- ``/out/<config>/<run>``, which scenario_execution derives per work
    item from its parameter document. A process the scenario merely *launched* (a simulator brought up
    by a ROS launch file, say) therefore has nowhere correct to drop a per-run artifact: writing to
    ``/out`` collides across runs, since every pod mirrors its ``/out`` into the same campaign prefix.

    So name it, for the case where it is unambiguous. With the default packing (``runs_per_job: 1``,
    :class:`OnePerJob`) that is every job; a packed job runs several work items sequentially and one
    variable cannot serve them all, so it is omitted rather than made wrong, and a consumer falls back
    to ``OUTPUT_DIR``.
    """
    items = getattr(job, "items", None) or []
    if len(items) != 1:
        return ()
    item = items[0]
    return (('RUN_OUTPUT_DIR', f"/out/{item.config_name}/{item.run_number}"),)


def probe_tag(node_id: str) -> str:
    """The file/tag stem for a node's calibration probe.

    Derived from the node's identity hash, so it names no machine -- the same reason the
    identity label exists -- and is stable, so a re-run finds the same file.
    """
    return f"probe-{node_id}"


#: The env var carrying extra flags to the scenario runner; the entrypoint appends it to the
#: command line verbatim.
SCENARIO_PARAMS_ENV = "SCENARIO_EXECUTION_PARAMETERS"

#: What makes a probe report on the scenario runner itself. See
#: :data:`~.node_calibration.PROBE_TICK_REFUSE_RATIO` for what the resulting file is used for.
TICK_LOG_FLAG = "--tick-log"


def probe_manifest(base: dict, *, job_name: str, params_file: str, output_dir: str,
                   display_name: str) -> dict:
    """A calibration probe's manifest, from the manifest a real job of this batch would use.

    Derived rather than built, because the measurement is only worth anything if the probe
    runs what the campaign runs -- same image, same containers, same scenario, same declared
    resources. Four things differ **in what the pod runs**, and nothing else does:

    * the Job's **name**, since it is not one of the batch's jobs;
    * ``SCENARIO_PARAMETER_FILE``, pointing at the probe's own document, whose ``_output_dir``
      keeps the scenario's results out of the run tree;
    * ``OUTPUT_DIR``, which keeps the job artifacts -- the monitor CSVs this exists to read --
      out of it too;
    * :data:`TICK_LOG_FLAG` on :data:`SCENARIO_PARAMS_ENV` -- the one difference that adds
      something rather than isolating something, and the reason it is confined to the probe:
      it is per-tick instrumentation on the trial's hot path, and the run that has to be
      validated is the one deciding the allocation.

    Both output overrides are needed: they govern different halves of what a job writes, and
    changing only one leaves the probe writing into a real campaign run directory.

    Rewritten across **every** container, not only the main one: the sidecars are handed the
    same extra env, and a sidecar still writing to the old ``OUTPUT_DIR`` would put the
    simulator's and the system under test's own CSVs -- the two that matter most here -- back
    into the run tree.

    Beside those three, two pieces of **identity**, which no container reads and which
    therefore leave the measurement untouched:

    * the :data:`~.manifests.JOB_KIND_LABEL` label, on the Job and on the pod template, so
      every reader can tell a probe from a trial without matching on its name;
    * its own ``job-name-full``, because the deepcopy would otherwise hand the probe the
      display label of the job it was derived from -- putting a second row with job 0's name
      into every listing, indistinguishable from the real one.
    """

    manifest = copy.deepcopy(base)
    meta = manifest.setdefault("metadata", {})
    meta["name"] = job_name
    # ``setdefault`` on both levels: a minimal base manifest (an offline emit, a test) carries
    # neither key, and this is metadata rather than a container edit, so there is nothing to
    # refuse over.
    meta.setdefault("labels", {})[JOB_KIND_LABEL] = CALIBRATION_JOB_KIND
    template_meta = manifest["spec"]["template"].setdefault("metadata", {})
    template_meta.setdefault("labels", {})[JOB_KIND_LABEL] = CALIBRATION_JOB_KIND
    template_meta.setdefault("annotations", {})["job-name-full"] = display_name
    spec = manifest["spec"]["template"]["spec"]
    overrides = {"SCENARIO_PARAMETER_FILE": params_file, "OUTPUT_DIR": output_dir}
    for container in list(spec.get("containers") or []) + list(spec.get("initContainers") or []):
        for entry in container.get("env") or []:
            if entry.get("name") in overrides and "value" in entry:
                entry["value"] = overrides[entry["name"]]
            # `--tick-log` on the PROBE only. It is per-tick instrumentation on the trial's
            # hot path, and the run it has to validate is the one that decides the
            # allocation -- so campaign runs keep it off and pay nothing for it. Appended
            # rather than assigned: a campaign may already be passing flags of its own.
            if entry.get("name") == SCENARIO_PARAMS_ENV and "value" in entry:
                if TICK_LOG_FLAG not in (entry["value"] or ""):
                    entry["value"] = f"{entry['value']} {TICK_LOG_FLAG}".strip()
    # ... and added where the campaign passed none, since there is then no entry to append to.
    for container in spec.get("containers") or []:
        env = container.setdefault("env", [])
        if not any(e.get("name") == SCENARIO_PARAMS_ENV for e in env):
            env.append({"name": SCENARIO_PARAMS_ENV, "value": TICK_LOG_FLAG})
        break
    return manifest


def _bootstrap_role(container_name: str, roles=()) -> str:
    """Which bootstrap figure *container_name* takes.

    The declared ROLE decides where there is one, the container's name otherwise -- the same
    precedence :func:`calibrated_resources` applies, and for the same reason: a stack that
    bundles its own simulator serves the simulation role from its ``sut`` container.
    """
    from robovast.common.config import CONTAINER_ROLES  # noqa: PLC0415

    for role in (roles or ()):
        if role in CONTAINER_ROLES:
            return role
    return container_name


def _with_bootstrap(declared: dict, container_name: str = None, roles=()) -> dict:
    """*declared* as-is, or the bootstrap figures where it says nothing.

    Reached whenever a container has no measurement yet -- the probe itself, every job on a
    node still being measured, and every job on a node whose probe was refused. Under
    ``sizing: fixed`` the declaration is always there and this returns it untouched.

    **The limit is written explicitly, never left empty.** ``JOB_TEMPLATE`` reads
    ``AVAILABLE_CPUS`` and ``AVAILABLE_MEM`` from ``resourceFieldRef: limits.cpu`` /
    ``limits.memory``, and the downward API substitutes the NODE's allocatable for an empty
    limit -- so a container would be told it has the whole machine, and ``/dev/shm``, sized
    from the same place, would turn an overrun into a SIGBUS with no reason attached. A
    wrong answer that looks right.
    """
    from .node_calibration import bootstrap_sizing  # noqa: PLC0415

    if (declared or {}).get("cpu") and (declared or {}).get("memory"):
        return declared
    cpu, memory = bootstrap_sizing(_bootstrap_role(container_name, roles))
    out = dict(declared or {})
    out.setdefault("cpu", cpu)
    out.setdefault("cpu_limit", out.get("cpu_limit") or cpu)
    out.setdefault("memory", memory)
    out.setdefault("memory_limit", out.get("memory_limit") or out["memory"])
    return out


def calibrated_resources(declared: dict, container_name: str, node_figures, roles=(),
                         bootstrap: bool = False, settings=None) -> dict:
    """*declared* re-sized for one node, or unchanged when that node is not calibrated yet.

    *settings* is the container's resolved calibration -- ``size_on`` having already chosen
    the statistic upstream, so what arrives here is one measured figure per resource and this
    function only has to place it. ``limit`` decides whether the ceiling follows the request
    or stays where it was declared.

    **CPU takes the measured figure; memory takes the measured maximum.** Both with their own
    headroom, both clamped to what the author declared, because calibration sizes a node's
    jobs *down* to what they need and has no business raising a ceiling someone set.

    **What ``limit: request`` buys is an IDENTICAL budget in every run, not a counter that
    reads zero.** A container sized at its own measurement sits against that measurement, so
    it throttles -- measured here, the system under test was quota-bound in every run of a
    150-run campaign, at up to 10.5%, against 2 runs in 45 for a declared figure two to three
    times larger. That is what a tight, measured ceiling looks like and not evidence of harm:
    over the same pair, realtime factor was *better* calibrated (0.9994 against 0.9941),
    errors were zero in both, and the verdict rate did not move.
    
    The property being protected is that the allocation is the same on every run of a node,
    so it cannot become a hidden variable between them. Sizing for a counter of zero instead
    would mean padding the largest container by 60-100%, which hands back most of the ~1.6x
    density that measuring exists to find, in exchange for a harm no signal detects.

    **A probe is judged far more strictly, and that is the asymmetry to keep in view.** It is
    refused outright for throttling past what its own statistic absorbs (see
    :func:`~.node_calibration.probe_refuse_ratio`), because a probe clipped while measuring
    reports its ceiling rather than its demand -- and a campaign sized from that would inherit
    it on every later run.

    The two resources are not symmetric and the asymmetry is the reason memory is read at the
    maximum whatever the role: exceeding a CPU reservation slows a container, exceeding a
    memory one kills it.
    """
    figures = (node_figures or {}).get(container_name)
    if not figures:
        return _with_bootstrap(declared, container_name, roles) if bootstrap else declared

    from .node_calibration import MIN_CPU  # noqa: PLC0415

    settings = settings or {}
    headroom = settings.get("headroom") or {}
    # The bootstrap is the BASE on this path too, not only where no figures exist. Only the
    # measured resources are overwritten below, so everything else -- memory where the probe
    # could not read it, and the ceiling under `limit: declared` -- has to come from
    # somewhere. Under `sizing: calibrated` `declared` is empty by definition, so building on
    # it alone leaves a container with no limit at all: the downward API then substitutes the
    # NODE's allocatable, the run is told it has the whole machine, and `/dev/shm` follows.
    out = dict(_with_bootstrap(declared, container_name, roles) if bootstrap else declared)
    ceiling = _declared_cores(declared) or _declared_cores(out)

    cores = figures.get("cores")
    if cores:
        cpu = max(MIN_CPU, round(cores * float(headroom.get("cpu") or 1.0), 3))
        out["cpu"] = min(cpu, ceiling) if ceiling else cpu
        if settings.get("limit") == "request":
            # Request == limit: the container never throttles, and its budget is the same in
            # every run of the campaign. What the system under test needs, and what makes a
            # clipped plan impossible rather than merely unlikely.
            out["cpu_limit"] = out["cpu"]
        else:
            # The ceiling stays where the author -- or the bootstrap -- put it: a soft limit
            # is what lets a burst through, and calibration is about the reservation.
            out.setdefault("cpu_limit", declared.get("cpu_limit") or declared.get("cpu"))

    peak_bytes = figures.get("memory_peak")
    if peak_bytes:
        sized = _memory_reservation(peak_bytes, float(headroom.get("memory") or 1.0))
        declared_bytes = _declared_bytes(declared) or _declared_bytes(out)
        if declared_bytes:
            sized = min(sized, declared_bytes)
        out["memory"] = str(sized)
        # Memory request == limit for every role. A soft memory ceiling buys nothing: a
        # container that exceeds its limit is killed rather than slowed, so "allowed to burst"
        # means "allowed to die", and the only safe reading of a measurement is one the
        # container is actually held to.
        out["memory_limit"] = out["memory"]
    return out


def _memory_reservation(peak_bytes: float, headroom: float) -> int:
    """*peak_bytes* with headroom, rounded up the way a reservation is written.

    Reuses ``advice``'s granularity and rounding rather than repeating the arithmetic: it is
    already the authority for memory sizing and reads the very counter this figure came from.
    """
    from robovast.results_processing.advice import (  # noqa: PLC0415
        MEM_GRANULARITY_BYTES, ceil_to)

    return int(ceil_to(peak_bytes * headroom, MEM_GRANULARITY_BYTES))


def _declared_bytes(declared: dict):
    """The memory ceiling *declared* states, in bytes, or ``None``."""
    from .kube_client import parse_resource  # noqa: PLC0415

    raw = (declared or {}).get("memory_limit") or (declared or {}).get("memory")
    if raw is None:
        return None
    try:
        return int(parse_resource(str(raw)))
    except Exception:  # noqa: BLE001 - an unparseable ceiling is no ceiling
        return None


def _declared_cores(declared: dict):
    """The container's declared CPU ceiling in cores, or ``None`` when it has none.

    ``cpu_limit`` when the author split request from limit, otherwise ``cpu`` -- which is what
    the limit equals when they did not. Unreadable returns ``None``, so an exotic per-cluster
    form leaves calibration exactly as it was rather than clamping against a number that is
    not one.
    """
    from robovast.common.quantity import to_cores  # noqa: PLC0415

    raw = declared.get("cpu_limit") or declared.get("cpu")
    if not raw:
        return None
    try:
        return to_cores(raw)
    except (TypeError, ValueError):
        return None


def stamp_resources(spec: dict, resources: dict) -> None:
    """Put *resources* on one container spec as separate requests and limits.

    ``cpu_limit`` / ``memory_limit`` are the ceiling and default to the reservation, which is
    what every campaign declared before they existed and keeps those manifests byte-identical.

    The two are stamped in one place for both the main container and the sidecars so they
    cannot drift: the request is what the cluster packs by -- and what RoboVAST's own
    admission measures a job with, via the rendered manifest -- while the limit only decides
    when the kernel starts throttling. Getting that backwards would either over-admit (pack by
    a ceiling nothing reserves) or throttle a container that reserved room it is not allowed to
    use.

    **Neither may be left empty.** ``JOB_TEMPLATE`` reads ``AVAILABLE_CPUS`` / ``AVAILABLE_MEM``
    from ``resourceFieldRef: limits.cpu / limits.memory``, and the downward API substitutes the
    NODE's allocatable for an unset limit -- so a scenario would size itself to the whole
    machine and be wrong in a way that looks right.
    """
    res = spec.setdefault('resources', {})
    requests = res.setdefault('requests', {})
    limits = res.setdefault('limits', {})
    for key, limit_key in (('cpu', 'cpu_limit'), ('memory', 'memory_limit')):
        request = resources.get(key)
        if not request:
            continue
        limit = resources.get(limit_key) or request
        requests[key] = str(request)
        limits[key] = str(limit)


def _short_job_name(campaign: str, config_name: str, run_number: int) -> str:
    """Create a short Kubernetes job name (max 63 chars) for campaign-id-config-run.

    Format: <name6>-<HHMMSScc>-<config6chars><sha256_16chars>-<run_number>
    - campaign: "<name>-2026-02-27-14113025"
        -> name prefix: first 6 lowercase alphanumeric chars of <name>
        -> time suffix: last 8 chars of timestamp (HHMMSScc) = "14113025"
      e.g. "dynamic_obstacle-2026-02-27-14113025" -> "dynami-14113025"
           "campaign-2026-02-27-14113025"         -> "campai-14113025"
    - config_name: first 8 alphanumeric for readability, rest as 4-char hash for uniqueness
    - run_number: as-is (e.g. 0, 1, ...)
    Labels keep full campaign-id for identifying.
    """
    # Extract "<name>" from "<name>-YYYY-MM-DD-HHMMSScc" (6-8 digit tail)
    ts_match = re.search(r'\d{4}-\d{2}-\d{2}-(\d{6,8})$', campaign)
    hhmmss = ts_match.group(1) if ts_match else campaign[-8:]
    # Strip the timestamp suffix to get the name prefix
    raw_name = re.sub(r'-\d{4}-\d{2}-\d{2}-\d{6,8}$', '', campaign) if ts_match else campaign
    name_alpha = re.sub(r'[^a-z0-9]', '', raw_name.lower())[:6]
    # Kubernetes names must start with a letter; fall back to 'r' if name is empty or starts with digit
    if not name_alpha or name_alpha[0].isdigit():
        name_alpha = 'r' + name_alpha[:5]
    run_part = f"{name_alpha}-{hhmmss}"

    # First 6 alphanumeric chars for readability + 16-char SHA-256 for collision-free uniqueness
    config_alpha = re.sub(r"[^a-zA-Z0-9]", "", config_name)[:6].lower()
    config_hash = hashlib.sha256(config_name.encode()).hexdigest()[:16]
    config_part = f"{config_alpha}{config_hash}" if config_alpha else config_hash

    return f"{run_part}-{config_part}-{run_number}"


def pullable_digest(image_id: str | None) -> str | None:
    """A pullable ``repo@sha256:…`` ref from a pod container's ``imageID``, or None.

    Kubernetes reports the exact image a container ran as ``status.imageID`` — usually
    ``registry/repo@sha256:<digest>``, sometimes prefixed (``docker-pullable://…``). We
    keep only refs that carry an ``@sha256:`` digest (a plain local ``sha256:…`` id is
    not pullable and is rejected), stripping any ``scheme://`` prefix.
    """
    if not image_id:
        return None
    ref = image_id.split("://", 1)[-1]
    return ref if "@sha256:" in ref else None


def resolve_image_digest(container_statuses, image: str) -> str | None:
    """The immutable digest ref of the SUT *image* from a run pod's container statuses.

    Prefers the container whose ``image`` matches the resolved SUT tag; falls back to
    any container that yields a pullable digest. Returns None when nothing usable is
    found (an old node, a missing status) — the caller then leaves the image unpinned
    rather than guessing.
    """
    statuses = list(container_statuses or [])
    for match_wanted in (True, False):
        for cs in statuses:
            if match_wanted and getattr(cs, "image", None) != image:
                continue
            digest = pullable_digest(getattr(cs, "image_id", None))
            if digest:
                return digest
    return None


def all_jobs_waiting_for_capacity(remaining, contended) -> bool:
    """Whether NOTHING of this batch can currently run because it is queued.

    True only when every remaining job is *contended* -- waiting its turn for capacity or
    for an image pull the kubelet is rate-limiting, both of which recover on their own. While
    that holds, no run of this campaign can complete, so the per-run no-progress deadline is
    measuring a queue rather than the campaign, and a reader must not return a verdict.

    False while ANY job can run: that job completing is exactly the progress the deadline
    watches for, so the verdict stands.

    False for a job that is *blocked* but not contended -- an image that does not exist, a
    request no node can hold. Those look the same in ten minutes as in one, and hiding them
    behind a queue that does not exist is how a dead campaign gets a health certificate.

    False when the probe could not read the cluster (``contended`` is None): unknown is not
    "queued", and treating it as such would silence the deadline exactly when the cluster is
    unreadable.
    """
    if not remaining or not contended:
        return False
    return all(job in contended for job in remaining)


class BatchJobRunner:
    """Build, submit and clean up the Kubernetes Jobs for **one** batch.

    Constructed only via :meth:`for_batch` from a pre-built ``campaign_data``
    (the controller has already composed it). Runs in-pod: storage I/O is direct
    (no archiver) and the Kubernetes client uses the in-cluster service account.
    Building manifests touches no API; only :meth:`run_batch_in_pod` does.
    """

    #: The process-wide admission queue, or ``None`` for "create every job at once".
    #: A class attribute so that every construction path has it -- ``for_batch`` sets it, and
    #: the offline callers that build a bare runner (manifest emit, ``vast prepare``, the
    #: tests) inherit the old behaviour without having to know the queue exists.
    admission = None

    #: Cooperative-stop signal, set by :meth:`for_batch`. Class-level default so a
    #: runner built another way (offline manifest emit, tests) has no stop wired.
    _state = None

    #: SUT image ref and the immutable digest captured from the run pods; class-level
    #: defaults so a runner built another way (offline manifest emit, tests) is safe.
    image = None
    _resolved_image_digest = None
    _resolved_image_digests: dict = {}

    #: Effective per-Job ``activeDeadlineSeconds`` and the set of Jobs already logged
    #: as hard-killed on it. Class-level defaults for runners not built via
    #: :meth:`for_batch`.
    _deadline_seconds = None
    _deadline_killed = frozenset()

    #: How long a batch tolerates jobs stuck unable to start (image pull / config
    #: error) before failing with the Kubernetes reason. The shared value, because the
    #: rollout wait and the image-build status read make the same trade -- see
    #: :data:`~.cluster_execution.BLOCKED_GRACE_SECONDS`.
    _BLOCKED_GRACE_SECONDS = BLOCKED_GRACE_SECONDS

    #: The longer tolerance for a job that only waits for a busy cluster -- see
    #: :data:`~.cluster_execution.CONTENDED_GRACE_SECONDS`.
    _CONTENDED_GRACE_SECONDS = CONTENDED_GRACE_SECONDS

    #: Jobs already dropped by :meth:`_drop_job` -- a restarted container or a pod that
    #: never started. Either keeps being reported on every poll until its pod is gone, and
    #: deleting a Job is asynchronous, so without this one fault is recorded -- and its
    #: trial discarded -- several times over. It is also the batch's tally of what it
    #: lost, which the end of :meth:`run_batch` reads to tell a flaky cluster from a fault
    #: every job of the batch shares. ``None`` here rather than a set because the class is
    #: populated attribute by attribute (see :meth:`for_batch`) and a mutable class
    #: attribute would be shared by every runner in the process; :meth:`_drop_job`
    #: replaces it with a per-instance set on first use.
    _invalidated = None
    #: How often a batch that is blocked repeats why, so a long wait for
    #: capacity stays visible in the log instead of scrolling past.
    _BLOCKED_LOG_INTERVAL_SECONDS = 60.0

    @classmethod
    def for_batch(cls, *, campaign_data, campaign_id, batch_tag, runs, cluster_config,
                  namespace, image, kube_context=None, log_tree=False, state=None,
                  built_images=None, image_digest_cache=None, admission=None):
        self = cls()
        # The process-wide admission queue, or None. None means "create every job at once",
        # which is what every offline caller (manifest emit, `vast prepare`, the tests) needs
        # and what the cluster lane did before the queue existed -- so this parameter arriving
        # changes nothing until a caller actually passes one.
        self.admission = admission
        self.cluster_config = cluster_config
        self.namespace = namespace
        # Used only for resolve_resources() (per-cluster resource lists); the
        # Kubernetes API client uses in-cluster config (see _ensure_k8s_initialized).
        self.kube_context = kube_context
        self.log_tree = log_tree
        # Cooperative-stop signal; ``None`` for offline callers (manifest emit, tests).
        self._state = state

        self.campaign = campaign_id
        self.campaign_data = campaign_data
        self.configs = campaign_data.get("configs", [])
        self.num_runs = runs
        # The SUT image ref the run pods use; captured back as an immutable digest
        # after the batch runs (see run_batch_in_pod) so postprocessing reuses the
        # exact image the runs recorded their bags with.
        self.image = image
        self._resolved_image_digest = None
        #: Outstanding calibration probes, ``{job key: node id}``. Kept apart from the
        #: batch's own jobs so a probe never reaches ``get_remaining_jobs`` alongside them.
        self._probes: "dict[str, str]" = {}
        #: This campaign's calibration, fetched once per batch. Held here rather than asked
        #: of the queue on each lookup, because the queue serves it under the lock it holds
        #: while draining -- see :meth:`_node_figures`.
        self._calibration = None
        self._resolved_image_digests = {}
        # Set for real by _pin_image_refs; the plain ref until then, so an offline caller
        # (manifest emit, tests) that never reaches a cluster still renders a valid pod.
        self._sidecar_image = resolve_sidecar_image()
        self._registry_ca_file = None
        # ``None`` ⇒ classic single-batch layout; the controller sets
        # a tag per search batch so jobs/param files/storage prefix don't collide.
        self._batch_tag = batch_tag

        execution_params = campaign_data.get("execution", {}) or {}
        #: ``fixed`` or ``calibrated`` -- see ``ExecutionConfig.sizing``. Read once here so
        #: the sizing path does not have to reach back into the campaign document, and so a
        #: runner built for an offline emit (which has no campaign) defaults to ``fixed``.
        # Inferred where the file does not say, by the same rule the model applies: what
        # reaches a backend is the PARSED YAML rather than the validated model, so reading
        # the key alone made every campaign that declared nothing run as `fixed` -- the
        # opposite of what declaring nothing asks for, and silently.
        from robovast.common.config import infer_sizing  # noqa: PLC0415
        self.sizing_mode = infer_sizing(execution_params)
        self.pre_command = execution_params.get("pre_command")
        self.post_command = execution_params.get("post_command")
        self.run_as_user = execution_params.get("run_as_user", 1000)

        # Declared before anything may reach the cluster: _pin_image_refs below asks the
        # registry, which needs these to exist. They stay None for every offline caller
        # (manifest emit, tests), whose _ensure_k8s_initialized finds no kubeconfig.
        self.k8s_client = None
        self.k8s_batch_client = None
        self.k8s_api_client = None
        self._k8s_initialized = False

        # One container plan, shared with the local lane and exec_in_container.
        self.plan = plan_containers(execution_params, images=built_images,
                                    explicit_main=image)
        # Before anything is put in a manifest: every ref these pods will run, resolved
        # to the bytes it names right now. Shared across the campaign's batches by the
        # backend, so a sweep asks the registry once and not once per batch.
        self._pin_image_refs(image_digest_cache)
        # Once per campaign, not per job: a `list_node()` per job would add an API call to
        # every one of a sweep's runs to answer a question whose answer cannot change
        # between them.
        self._discover_gpu_support()
        # Builds self.manifest and sets self.env.
        self.manifest = self.get_job_manifest(
            self.plan.main.image or image,
            self.plan.main.resources or {},
            execution_params.get("env", []),
            self.run_as_user,
        )
        # Always cap a Job's wall-clock time so a scenario that never shuts itself
        # down is force-killed by Kubernetes (``DeadlineExceeded``) instead of hanging
        # the campaign forever. ``execution.timeout`` is the budget for a whole Job, so
        # it is used as declared -- this is the granularity Kubernetes can enforce at, and
        # multiplying it out here would reconstruct a per-run figure nothing can act on.
        # The figure comes from ``common.config`` because the campaign status derives its
        # stall threshold from the same declaration -- were the two to diverge, a Job could
        # be killed while the status still called it healthy.
        self._deadline_seconds = job_deadline_seconds(execution_params)
        self.manifest["spec"]["activeDeadlineSeconds"] = self._deadline_seconds
        # Pod-level, so it has to be decided across every container of the plan rather
        # than inside get_job_manifest -- which sees only the main container and would
        # therefore lose the case that matters most, a GPU on the simulation sidecar.
        self._apply_pod_gpu_runtime()
        # Jobs already logged as hard-killed on the deadline, so the wait loop warns
        # once per job rather than every poll.
        self._deadline_killed = set()

        return self

    def _ensure_k8s_initialized(self):
        """Initialise Kubernetes clients from the in-cluster service account."""
        if self._k8s_initialized:
            return
        from .kube_client import load_kube_config  # pylint: disable=import-outside-toplevel

        # In-cluster in the service pod; host kubeconfig for host-side dry-runs / tests.
        load_kube_config(context=self.kube_context)
        self.k8s_client = client.CoreV1Api()
        self.k8s_batch_client = client.BatchV1Api()
        self.k8s_api_client = client.ApiClient()
        self._k8s_initialized = True

    # -- manifest toolkit ---------------------------------------------------

    def replace_template(self, elem, tmpl, idx):
        if isinstance(elem, dict):
            for key, value in elem.items():
                elem[key] = self.replace_template(value, tmpl, idx)
        elif isinstance(elem, list):
            for i, item in enumerate(elem):
                elem[i] = self.replace_template(item, tmpl, idx)
        elif isinstance(elem, str):
            elem = elem.replace(tmpl, str(idx))
        return elem

    def _s3_settings(self):
        """Return (endpoint, access_key, secret_key, bucket_name, campaign_prefix).

        ``campaign_prefix`` is ``"<campaign>/"`` for shared-bucket backends
        (e.g. GCS) and ``""`` for per-campaign buckets (embedded MinIO). It is the
        flat campaign prefix — batches share it (no ``_batches/`` component); cross-
        batch collisions are prevented by the batch-namespaced ``_job_tag`` (job
        names, ``<tag>.params.yaml``, ``_jobs/<tag>``), so the layout matches local.
        """
        s3_endpoint = self.cluster_config.get_s3_endpoint()
        s3_access_key, s3_secret_key = self.cluster_config.get_s3_credentials()
        bucket_name, campaign_prefix = in_pod_storage.campaign_storage_location(
            self.cluster_config, self.campaign)
        return s3_endpoint, s3_access_key, s3_secret_key, bucket_name, campaign_prefix

    def _job_tag(self, index: int) -> str:
        """Flat, slash-free job tag for job *index*, namespaced by the batch when set.

        Used for the (globally unique, K8s-safe) job name and the
        ``<tag>.params.yaml`` file, so these never collide across batches.

        **The batch tag is flattened, because it is not always flat.** A batch whose
        parameter sets ask for different repetition counts is tagged ``batch-<n>/reps-<k>``
        -- the grouping is real and the slash belongs in ``_jobs/``, where it is a directory
        (see :meth:`_job_artifact_path`). It cannot survive here: this tag names a *file*,
        where the slash became an unmade directory and the campaign died on
        ``_transient/batch-1/reps-3-job-0.params.yaml`` before its first run, and a
        Kubernetes Job, where a slash is not a legal DNS-1123 label. This method promised
        "flat, slash-free" and left it to its caller to be true; only a campaign with
        non-uniform repetitions ever made it false.
        """
        if not self._batch_tag:
            return f"job-{index}"
        # Replaced rather than stripped, so ``batch-1/reps-3`` and ``batch-1/reps-5`` stay
        # distinct -- they are different jobs and this tag is what keys them apart.
        return f"{self._batch_tag.replace('/', '-')}-job-{index}"

    def _job_artifact_path(self, index: int) -> str:
        """Path of the job's artifact dir under ``_jobs/`` (no leading ``_jobs/``).

        Nested ``<batch>/job-<idx>`` when batched (matching the local layout), else
        flat ``job-<idx>``. This is the symlink target base used by ``job_links``.
        """
        return job_artifact_rel(index, self._batch_tag)

    def _build_job_manifest(self, *, job_short_name, job_full_name, item_tag,
                            sim_overlay=None, node_figures=None,
                            total_jobs, s3_prefix, init_cmd, extra_main_env=()):
        """Assemble a job manifest shared by single-config and packed jobs.

        The two paths differ only in job naming, the S3 output prefix, the
        initContainer mirror command, and a few extra env vars
        (``extra_main_env``); everything else (volumes, the init container, the
        main container env, secondary containers) is identical and lives here.
        """
        job_manifest = copy.deepcopy(self.manifest)

        label_safe_campaign = _label_safe_campaign(self.campaign)
        self.replace_template(job_manifest, "$CAMPAIGN_ID", label_safe_campaign)
        self.replace_template(job_manifest, "$JOB_NAME", job_short_name)
        self.replace_template(job_manifest, "$JOB_FULL_NAME", job_full_name)
        self.replace_template(job_manifest, "$ITEM", item_tag)
        self.replace_template(job_manifest, "$TOTAL_JOB_NUM", str(total_jobs))

        s3_endpoint, s3_access_key, s3_secret_key, bucket_name, campaign_prefix = self._s3_settings()

        spec = job_manifest['spec']['template']['spec']
        if node_figures or self._sizing_is_calibrated():
            # The MAIN container too, not only the sidecars. Its resources were stamped once
            # onto the base manifest at batch setup, so without this the container running
            # the scenario would be the one container a calibrated node did not re-size --
            # and on the ROS shape that is the scenario, a third of the pod.
            #
            # `node_figures` alone is not the condition, and the difference is the whole
            # first job on every node: before anything is measured there are no figures, so
            # gating on them left the main container with NO resources at all rather than
            # the bootstrap. An empty limit is not merely generous -- JOB_TEMPLATE reads
            # AVAILABLE_CPUS/AVAILABLE_MEM from `resourceFieldRef: limits.*`, and the
            # downward API substitutes the NODE's allocatable for an absent limit, so the
            # scenario sizes itself to the whole machine and the probe measures a container
            # that was never bounded.
            main_role = getattr(getattr(self, "plan", None), "main", None)
            main_name = getattr(main_role, "name", None) or SCENARIO_CONTAINER
            declared = {}
            requests = (spec['containers'][0].get('resources') or {}).get('requests') or {}
            if requests.get('cpu'):
                declared['cpu'] = requests['cpu']
            if requests.get('memory'):
                declared['memory'] = requests['memory']
            limits = (spec['containers'][0].get('resources') or {}).get('limits') or {}
            if limits.get('cpu'):
                declared['cpu_limit'] = limits['cpu']
            if limits.get('memory'):
                declared['memory_limit'] = limits['memory']
            # `declared` is empty under `sizing: calibrated`, and that is the case the
            # bootstrap exists for -- so the guard asks whether anything will be produced,
            # not whether anything was written in the file.
            # Looked up under MAIN_CONTAINER_NAME, not under the role: the probe measures
            # what the monitor wrote, and the main container's file is `resource_usage_main`
            # whatever the `.vast` calls the container -- the same mismatch
            # `_probe_container_limits` aliases past. Keyed by the role instead, the lookup
            # misses on every node and the container silently keeps the bootstrap for the
            # whole campaign, which is not a visible failure: it is a container running at a
            # figure nobody chose, throttling against it.
            #
            # The ROLE still decides the statistic and the bootstrap, so it is passed on
            # separately -- falling back to the scenario role, since that is what the main
            # container runs when the `.vast` names no block for it.
            sized = calibrated_resources(declared, MAIN_CONTAINER_NAME, node_figures,
                                         roles=(getattr(main_role, "roles", ())
                                                or (main_name,)),
                                         bootstrap=self._sizing_is_calibrated(),
                                         settings=self._calibration_by_container().get(
                                             MAIN_CONTAINER_NAME))
            if sized:
                stamp_resources(spec['containers'][0], sized)

        # Tolerate the taint a campaign node may carry, on the pod itself: nothing else
        # injects it, and a deployment that taints its campaign nodes without it does not
        # fail loudly -- its pods simply never place. Additive and idempotent, so it is
        # safe to apply to a spec that already carries it.
        from .node_placement import CAMPAIGN_NODE_TOLERATIONS  # noqa: PLC0415
        existing = list(spec.get('tolerations') or [])
        for toleration in CAMPAIGN_NODE_TOLERATIONS:
            if dict(toleration) not in existing:
                existing.append(dict(toleration))
        spec['tolerations'] = existing

        # Pull secret for an agent-built experiment image pushed to a private
        # registry (see RegistryConfig). Only present when a registry with auth was
        # configured at setup; a public/insecure registry needs none. Falls back to the
        # well-known Secret setup creates when the env var naming it is absent — which is
        # the normal case for an off-cluster service, since setup writes that name into the
        # deployed pod's env (see ClusterService._resolve_registry_objects).
        try:
            pull_secret = self.cluster_config.get_registry_config().pull_secret_name
            if not pull_secret:
                from .service_deploy import REGISTRY_PUSH_SECRET_NAME
                try:
                    self.k8s_client.read_namespaced_secret(
                        REGISTRY_PUSH_SECRET_NAME, self.namespace)
                    pull_secret = REGISTRY_PUSH_SECRET_NAME
                except client.exceptions.ApiException:
                    pull_secret = ""
        except Exception:  # noqa: BLE001 - registry config is optional
            pull_secret = ""
        if pull_secret:
            spec['imagePullSecrets'] = [{'name': pull_secret}]

        # Hosts the cluster's DNS cannot resolve (ROBOVAST_EXTRA_HOST_ALIASES). This
        # covers what the *pod* resolves; the image pull itself is the node's container
        # runtime, which no pod spec reaches — see BaseConfig.get_host_aliases.
        try:
            host_aliases = self.cluster_config.get_host_aliases()
        except Exception:  # noqa: BLE001 - never block a run on an optional alias list
            logger.warning("could not resolve host aliases for the campaign Job",
                           exc_info=True)
            host_aliases = []
        if host_aliases:
            spec['hostAliases'] = host_aliases

        # Volumes: config (populated by initContainer), out (shared output), dshm (shared /dev/shm),
        # ipc (named sockets between main and secondary containers)
        #
        # `dshm` is mounted into EVERY container of the pod, which is what lets ROS 2's
        # default Fast DDS use its shared-memory transport between the scenario, the sut
        # and the simulator. Without a `sizeLimit` a memory-backed emptyDir is sized from
        # the pod's memory limits -- and a campaign that declares none is therefore handed
        # the whole node's memory as its /dev/shm, charged to whichever container faults
        # the page. Overrunning it kills that container with SIGBUS (exit 135), not with a
        # clean OOMKilled, which is why such a death arrives unexplained. `execution.shm_size`
        # bounds it; unset keeps the previous behaviour so no existing campaign changes.
        dshm = {'medium': 'Memory'}
        shm_size = (self.campaign_data.get('execution', {}) or {}).get('shm_size')
        if shm_size:
            dshm['sizeLimit'] = shm_size
        spec['volumes'] = [
            {'name': 'config', 'emptyDir': {}},
            {'name': 'out', 'emptyDir': {}},
            {'name': 'dshm', 'emptyDir': dshm},
            {'name': 'ipc', 'emptyDir': {}},
            {'name': 'tmp', 'emptyDir': {}},
        ]

        init_env = [
            {'name': 'S3_ENDPOINT', 'value': s3_endpoint},
            {'name': 'S3_BUCKET', 'value': bucket_name},
            {'name': 'S3_ACCESS_KEY', 'value': s3_access_key},
            {'name': 'S3_SECRET_KEY', 'value': s3_secret_key},
            {'name': 'S3_CAMPAIGN_PREFIX', 'value': campaign_prefix},
        ]
        spec['initContainers'] = [
            {
                'name': 's3-init',
                'image': self._sidecar_image,
                'imagePullPolicy': pull_policy_for(self._sidecar_image),
                'command': ['sh', '-c', init_cmd],
                'env': init_env,
                'volumeMounts': [
                    {'name': 'config', 'mountPath': '/config'}
                ],
            }
        ]

        shared_volume_mounts = [
            {'name': 'config', 'mountPath': '/config', 'readOnly': True},
            {'name': 'out', 'mountPath': '/out', 'readOnly': False},
            {'name': 'dshm', 'mountPath': '/dev/shm'},
            {'name': 'ipc', 'mountPath': '/ipc'},
            {'name': 'tmp', 'mountPath': '/tmp'},
        ]

        # Add environment variables and volume mounts to the main (robovast) container
        containers = spec['containers']
        scenario_file_name = os.path.basename(
            self.campaign_data.get('scenario_file', 'scenario.osc')
        )
        if containers:
            if 'env' not in containers[0]:
                containers[0]['env'] = []

            env_vars = get_execution_env_variables(0, item_tag)
            for name, val in env_vars.items():
                containers[0]['env'].append({
                    'name': str(name),
                    'value': "" if val is None else str(val)
                })

            # S3 env vars for entrypoint post-run upload
            for k, v in _s3_env(s3_endpoint, bucket_name, s3_access_key,
                                s3_secret_key, s3_prefix):
                containers[0]['env'].append({'name': k, 'value': v})

            # Add PRE_COMMAND and POST_COMMAND if specified
            if self.pre_command:
                containers[0]['env'].append({'name': 'PRE_COMMAND', 'value': str(self.pre_command)})
            if self.post_command:
                containers[0]['env'].append({'name': 'POST_COMMAND', 'value': str(self.post_command)})
            if self.log_tree:
                containers[0]['env'].append({'name': 'SCENARIO_EXECUTION_PARAMETERS', 'value': '-t'})

            # SCENARIO_FILE / SIMULATION / SCENARIO_MODE, derived from the .vast by the
            # same helper the local lane and container-exec use.
            for name, val in scenario_env(self.campaign_data).items():
                containers[0]['env'].append({'name': name, 'value': str(val)})

            for k, v in extra_main_env:
                containers[0]['env'].append({'name': k, 'value': v})

            containers[0]['volumeMounts'] = shared_volume_mounts

        # Add secondary containers (they receive the same packed env so a
        # sim/SUT server resolves file-valued reset parameters identically).
        for sc in self.plan.sidecars:
            sc_name = sc.name
            sc_resources = calibrated_resources(
                resolve_resources(sc.resources, self.kube_context), sc_name, node_figures,
                roles=getattr(sc, "roles", ()), bootstrap=self._sizing_is_calibrated(),
                settings=self._calibration_by_container().get(sc_name))
            secondary_env = [
                {'name': 'CONTAINER_NAME', 'value': sc_name},
                {'name': 'SCENARIO_FILE', 'value': scenario_file_name},
            ]
            for k, v in extra_main_env:
                secondary_env.append({'name': k, 'value': v})
            for env_var in self.env:
                if isinstance(env_var, dict):
                    for key, value in env_var.items():
                        secondary_env.append({'name': key, 'value': str(value)})
            # A sidecar with no command runs the scenario-execution server, so the
            # scenario can drive it with remote(). One that declares a command runs that
            # instead -- a simulator, or a stack RoboVAST does not drive. BOTH go through
            # secondary_entrypoint.sh: it sources the ROS overlay, tees stdout into the
            # job's log dir and starts the resource monitor, none of which a container
            # gets when its command is exec'd as the entrypoint. Passing the command by
            # env rather than as argv is what lets the same entrypoint serve both.
            # The backend's own env, for the container the backend describes. scenario_env
            # puts it on the main container, which is only right when the simulator IS the
            # main container (the stepped shape); in the ROS shape it is this sidecar.
            # The job's own resolved values win over the campaign default: a world belongs
            # to a configuration, and this sidecar is running one.
            sc_backend_env = dict(sidecar_backend_env(
                self.campaign_data.get('execution') or {}, sc_name))
            if sc_name == SIMULATION_CONTAINER and sim_overlay:
                sc_backend_env.update(sim_overlay.get('env') or {})
            for key, value in sc_backend_env.items():
                secondary_env.append({'name': key, 'value': value})
            # So this sidecar can run /tmp/s3_upload.sh once its workload has exited.
            for k, v in _s3_env(s3_endpoint, bucket_name, s3_access_key,
                                s3_secret_key, s3_prefix):
                secondary_env.append({'name': k, 'value': v})
            # The simulator's command is the one per-configuration thing in the plan: it
            # names the world. Everything else about this container -- image, resources,
            # packages -- stays campaign-level.
            sc_cmd = sc.command
            if sc_name == SIMULATION_CONTAINER and sim_overlay and sim_overlay.get('command'):
                sc_cmd = sim_overlay['command']
            if sc_cmd:
                secondary_env.append({'name': 'ROBOVAST_CONTAINER_COMMAND',
                                      'value': shlex.join(list(sc_cmd))})
            secondary_spec = {
                'name': sc_name,
                'image': sc.image,
                'imagePullPolicy': pull_policy_for(sc.image),
                # A NATIVE SIDECAR: an init container with restartPolicy Always. That is
                # what ties the pod's lifetime to the scenario rather than to whichever
                # container happens to run longest. As an ordinary container, a sidecar
                # running a simulator never exits -- a simulator has no reason to -- so the
                # Job stayed at 0/1 forever after the scenario had finished and uploaded
                # its results, and the controller polled "1/1 job(s) still running" until
                # something killed it. The command-less sidecars hid this: their
                # scenario-execution server has a --watchdog that ends it when the client
                # disconnects, so `sut` stopped and only `simulation` was left behind.
                #
                # Kubelet starts native sidecars before the regular containers and
                # terminates them after the last one exits, so this also fixes the
                # start-up order for free: the simulator is up before the scenario.
                # Stable since k8s 1.29.
                'restartPolicy': 'Always',
                'command': ['/usr/bin/tini', '--', '/bin/bash',
                            '/config/secondary_entrypoint.sh'],
                'env': secondary_env,
                'resources': {
                    'requests': {},
                    'limits': {},
                },
                'volumeMounts': shared_volume_mounts,
            }
            stamp_resources(secondary_spec, sc_resources)
            self._apply_gpu_to_container(secondary_spec, secondary_env,
                                         self._gpu_request(sc_resources, sc))
            if self.run_as_user is not None:
                secondary_spec.setdefault('securityContext', {})['runAsUser'] = self.run_as_user
            # Appended AFTER s3-init, which is an ordinary init container and therefore
            # runs to completion first -- it is what populates /config, and a sidecar
            # reads secondary_entrypoint.sh from there.
            spec['initContainers'].append(secondary_spec)

        return job_manifest

    def create_job_manifest(self, job, total_jobs: int, node_figures=None) -> dict:
        """Create a manifest for one job (1..K configs).

        One K8s Job runs all the job's configs via a multi-document param file
        (the simulator is reset between them). ``/out`` is this pod's emptyDir shaped
        as the campaign root, uploaded to the campaign prefix, so per-config
        results land at ``<campaign>/<config>/<run>/`` via each document's
        ``_output_dir``. Job-level artifacts go to a per-job subdir, and each
        config's files are mirrored under ``/config/<config-name>/`` to avoid
        collisions. The job's multi-document param file ships in ``_transient/``
        and so lands at ``/config/<job-tag>.params.yaml``.
        """
        _, _, _, _, campaign_prefix = self._s3_settings()
        job_tag = self._job_tag(job.index)
        sim_overlay = self._sim_overlay(job)
        # The simulator's overrides document ships per job (``<job-tag>.sim.yaml``, unique
        # like the parameter file) but is READ at a fixed path, because a backend builds
        # its command before any job exists and argv cannot expand an environment
        # variable. Locally the two are reconciled by the bind mount's target; here the
        # whole ``_transient/`` prefix is mirrored wholesale, so the reconciliation is this
        # one copy.
        sim_rename = (
            f"(cp /config/{job_tag}.sim.yaml {SIM_OVERRIDES_MOUNT} 2>/dev/null || true); "
            if sim_overlay["document"] else "")
        per_config_mirror = "".join(
            f"(mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}{cn}/_config/ /config/{cn}/ 2>/dev/null || true); "
            for cn in job.config_names
        )
        init_cmd = (
            f"mc alias set mystore \"$S3_ENDPOINT\" \"$S3_ACCESS_KEY\" \"$S3_SECRET_KEY\" && "
            f"mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}_config/ /config/ && "
            f"mc mirror mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}_transient/ /config/ && "
            f"{per_config_mirror}"
            f"{sim_rename}"
            f"for s3pfx in ${{S3_CAMPAIGN_PREFIX}}_config ${{S3_CAMPAIGN_PREFIX}}_transient; do "
            f"mc find mystore/$S3_BUCKET/$s3pfx/ 2>/dev/null | while IFS= read -r obj; do "
            f"mc stat --json \"$obj\" 2>/dev/null | grep -qi 'executable.*yes' && "
            f"chmod +x \"/config/${{obj#mystore/$S3_BUCKET/$s3pfx/}}\" 2>/dev/null || true; "
            f"done; done; true"
        )
        extra_env = (
            ('SCENARIO_PARAMETER_FILE', f"/config/{job_tag}.params.yaml"),
            ('OUTPUT_RESULT_PER_SCENARIO', 'true'),
            # Job artifacts land in the nested _jobs/<batch>/job-<idx> layout
            # (matching local), while the K8s job name / param file stay flat
            # (slash-free, globally unique). See _job_artifact_path / _job_tag.
            ('OUTPUT_DIR', f"/out/_jobs/{self._job_artifact_path(job.index)}"),
            ('SCENARIO_OUTPUT_DIR', '/out'),
        ) + _run_output_dir_env(job)
        return self._build_job_manifest(
            job_short_name=_short_job_name(self.campaign, job_tag, job.index),
            job_full_name=f"{self.campaign}-{job_tag}",
            item_tag=job_tag,
            total_jobs=total_jobs,
            s3_prefix=campaign_prefix.rstrip("/"),
            init_cmd=init_cmd,
            extra_main_env=extra_env,
            sim_overlay=sim_overlay,
            node_figures=node_figures,
        )

    def _sim_overlay(self, job) -> dict:
        """This job's resolved simulator command, environment and overrides document.

        ``job.items[0]`` speaks for the whole job: the packer groups work items by
        ``sim_key``, so a job that mixed simulator settings cannot be built. Asked of the
        backend with the job's own block, which is why the world differs per job while the
        image, the resources and the container set do not.
        """
        return sim_job_overlay(self.campaign_data.get("execution") or {},
                               job.items[0].config.get("sim") or {},
                               os.path.dirname(self.campaign_data.get("vast") or ""))

    def _runs_per_job(self) -> int:
        """How many runs (config × run-number work items) to pack into one job."""
        return int((self.campaign_data.get("execution") or {}).get("runs_per_job") or 1)

    def _build_jobs(self):
        """Group (config, run) work items into jobs per runs_per_job.

        Deterministic, so the jobs used to write per-job param files match the
        jobs used to create job manifests.
        """
        return build_jobs(self.configs, self.num_runs, self.campaign_data.get("execution") or {})

    @staticmethod
    def _jobs_already_done(jobs, campaign_root: str) -> set:
        """Indices of *jobs* whose every run already has a verdict under *campaign_root*.

        Empty for a campaign starting now -- the root is bare, so this is one ``isfile``
        miss per job and the batch behaves exactly as it always did. It is not empty for a
        campaign being **re-entered**: one whose driver a service restart took away, whose
        root has been restored from the object store, and whose jobs are therefore about to
        be planned a second time. Creating those again would re-run work that is finished
        and overwrite its results.

        The verdict, not the presence of a job artifact directory: ``test.xml`` is the
        evidence ``_run_batch_mode`` builds the store from and
        ``reconstruct_status_from_disk`` decides finished-vs-crashed on, so using anything
        else here would let two readers disagree about the same run. A job whose items
        landed *partly* is therefore not done, and is re-created whole -- the honest
        granularity, since a packed job's items share one simulator process and there is no
        way to re-enter it halfway.
        """
        done = set()
        for job in jobs:
            if all(os.path.isfile(os.path.join(campaign_root, item.config_name,
                                               str(item.run_number), "test.xml"))
                   for item in job.items):
                done.add(job.index)
        return done

    def _write_job_param_files(self, out_dir, campaign_root=None):
        """Write one multi-document scenario-parameter file per packed job into
        ``out_dir/_transient/`` so they upload with the campaign and are mirrored
        into each packed job's ``/config`` as ``job-<idx>.params.yaml``."""
        # Already resolved against the .vast's location by config generation (same note as in
        # execute_local); prepending the .vast's directory again doubles it whenever the project's
        # config path has a directory part.
        scenario_path = self.campaign_data["scenario_file"]
        scenario_name = next(iter(get_scenario_parameters(scenario_path).keys()))
        transient_dir = os.path.join(out_dir, "_transient")
        os.makedirs(transient_dir, exist_ok=True)
        jobs = self._build_jobs()
        for job in jobs:
            docs = build_job_parameter_documents(job, scenario_name)
            with open(os.path.join(transient_dir, f"{self._job_tag(job.index)}.params.yaml"), "w") as f:
                f.write(dump_multi_document_yaml(docs))
            # The simulation channel's per-job document. Single-document, because the
            # packer groups by `sim_key` and a job's items therefore agree on it.
            document = self._sim_overlay(job)["document"]
            if document:
                with open(os.path.join(transient_dir,
                                       f"{self._job_tag(job.index)}.sim.yaml"), "w") as f:
                    yaml.dump(document, f, default_flow_style=False, sort_keys=False)
        # Canonical link manifest, consumed by the controller's upload-to-share
        # compression to materialise <config>/<run>/job symlinks into the tar.gz, and
        # by readers resolving a job's artifacts while it is still running. Written in
        # per-batch mode too: the manifest is batch-aware, so the batch-namespaced job
        # tag does not break the target path.
        #
        # Seeded with what the campaign already has, because THIS DIRECTORY IS UPLOADED TO
        # THE CAMPAIGN PREFIX: a manifest holding only this batch overwrites the campaign's
        # at the same key, and every earlier batch's runs stop resolving. The damage is not
        # even consistent -- ``download_prefix`` skips a file whose local size matches the
        # remote, so a same-sized batch-only copy is sometimes skipped and sometimes not, and
        # a four-batch campaign was observed keeping exactly its last two batches.
        # *campaign_root* is absent only for the one-shot template dir (``vast prepare``),
        # which has no campaign to accumulate onto.
        write_job_links_manifest(
            transient_dir, jobs, self._batch_tag,
            base=read_job_links(campaign_root) if campaign_root else None)
        # A calibration probe's parameter document, one per node. Written HERE so it uploads
        # with everything else: a probe is created later, while the batch is already running,
        # and its /config is mirrored from this same prefix.
        #
        # Deliberately NOT added to the job-link manifest above. That manifest is what makes a
        # job's artifacts resolvable from <config>/<run>/job, and a probe has no run to
        # resolve from -- it is not one of the campaign's runs.
        self._write_probe_param_files(transient_dir, jobs, scenario_name)

    #: Probes are queued under their own owner, never the campaign's. ``states(campaign)``
    #: feeds ``created_names``, which feeds ``get_remaining_jobs`` and ``jobs_by_name`` -- and
    #: a probe is in none of those, so letting its name in there would have the batch loop
    #: asking about a job it has no plan for.
    _PROBE_OWNER_SUFFIX = "#probes"

    def _probe_owner(self) -> str:
        return f"{self.campaign}{self._PROBE_OWNER_SUFFIX}"

    def _start_probes(self, jobs, total_jobs, campaign_prefix):
        """Queue one calibration probe per uncalibrated node. Returns the calibration, or None.

        Each probe is **pinned** to the node it measures and runs at the DECLARED sizing --
        it is the thing that decides what that node's sizing should be, so it cannot use it.

        Queued at a higher priority than the campaign's own work, though the gate already
        keeps that work off an unmeasured node: a probe that loses the race for capacity to
        another campaign would leave this one waiting on a node nothing is measuring.
        """
        from functools import partial  # noqa: PLC0415

        from .node_admission import campaign_start_key  # noqa: PLC0415
        from .node_calibration import NodeCalibration, probe_output_dir  # noqa: PLC0415

        admission = self.admission
        if admission is None:
            return None
        self._warn_about_containers_with_no_role()
        calibration = admission.calibration(self.campaign, NodeCalibration)
        self._calibration = calibration
        node_ids = self._probe_node_ids(total_jobs)
        if not node_ids or not jobs:
            return calibration
        sizing = self._job_sizing(jobs[0], total_jobs)
        # **A probe no node could ever hold is a configuration fault, not a wait.** Under
        # calibrated sizing a probe runs at the bootstrap -- a deployment-wide default rather
        # than anything this campaign chose -- so it can exceed the smallest node without the
        # `.vast` saying anything wrong. Unchecked, the probe simply never places, and because
        # a node being measured accepts no work, that node sits idle for the whole batch with
        # nothing said. Checked here for the same reason the batch's own sizing is checked
        # before it is enqueued.
        try:
            admission.preflight(sizing)
        except Exception as exc:  # noqa: BLE001 - re-raised with what makes it actionable
            raise CampaignConfigError(
                f"A calibration probe needs {sizing.cpu:g} cpu and no node can hold it. Under "
                "execution.sizing: calibrated a probe runs at the deployment's bootstrap "
                "(ROBOVAST_BOOTSTRAP_CPU / _MEMORY) summed over this campaign's containers, "
                "so this is the cluster's default being larger than its smallest node rather "
                "than anything the campaign declared. Lower the bootstrap for a role, or "
                "declare execution.containers.<name>.resources so the probe is sized from the "
                f"campaign instead. ({exc})") from exc
        base = self.create_job_manifest(jobs[0], total_jobs)
        started = campaign_start_key(self.campaign)
        for index, node_id in enumerate(node_ids):
            key = _short_job_name(self.campaign, probe_tag(node_id), index)
            if not calibration.claim_probe(node_id, key):
                continue
            self._probes[key] = node_id
            admission.submit(
                self._probe_owner(),
                [(key, sizing,
                  partial(self._create_probe, base, key, probe_output_dir(node_id)))],
                started_at=started, priority=1, pin=node_id)
            logger.info("Batch %s: measuring node %s before placing work on it",
                        self._batch_tag, node_id)
        return calibration

    def _pin(self, manifest, node_id):
        """Confine the pod to the operator's node pool, then to the node admission chose.

        Both, in that order, and both ANDed onto whatever the spec already carried. The pool
        is ``execution.kubernetes.jobs.node_labels``, which is a pod ``nodeSelector``.

        The pool must reach the pod, not just the accounting: the budget provider counts only
        nodes inside it, so a pod free to land outside would be running on capacity nothing
        reserved. The pin then narrows the pool rather than widening it -- a selector that
        replaced the pool would defeat the very confinement it was placed inside.
        """
        pool = job_node_pool()
        if not pool and not node_id:
            # Nothing to confine. Returned untouched rather than reaching into the manifest:
            # this is called unconditionally, and a caller with a minimal manifest (an offline
            # emit, a test) would otherwise die on a key it never needed.
            return manifest
        spec = manifest.setdefault('spec', {}).setdefault(
            'template', {}).setdefault('spec', {})
        selector = {**(spec.get('nodeSelector') or {}), **pool}
        if node_id:
            selector[NODE_ID_LABEL] = node_id
        spec['nodeSelector'] = selector
        return manifest

    def _create_probe(self, base, key, output_dir, node_id=None):
        """Create one probe Job. Signature matches the queue's create callback."""
        # The node names the probe as well as its parameter file: ``_job_display_name`` only
        # strips the campaign prefix, so this string is what the job listing shows. It is the
        # identity hash ``probe_tag`` is built from, which names no machine.
        node = node_id or self._probes.get(key)
        manifest = probe_manifest(
            base, job_name=key,
            params_file=f"/config/{probe_tag(node)}.params.yaml",
            output_dir=f"/out/{output_dir}",
            display_name=f"calibration probe · {node}")
        self._pin(manifest, node_id)
        try:
            self.k8s_batch_client.create_namespaced_job(namespace=self.namespace,
                                                        body=manifest)
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                raise

    def _node_figures(self, node_id):
        """This campaign's calibrated figures for *node_id*, or ``None``.

        One lookup used by BOTH the manifest and the queue's arithmetic, so the two cannot
        disagree about what a job on that node costs.

        **Reads the calibration this runner already holds, and must never ask the queue for
        it.** The queue hands this out under its own lock, and one caller of this is the
        ``sizing_for_node`` callback -- which the queue invokes from inside ``drain``, with
        that lock held. Going back through the queue therefore deadlocks a non-reentrant
        lock, and the symptom is the worst kind: the batch loop never completes its first
        iteration, so nothing is created, nothing is logged, and the campaign simply sits
        there until its no-progress deadline calls it stalled. Observed exactly that way.
        """
        if not node_id or self._calibration is None:
            return None
        return self._calibration.calibrated(node_id)

    def _sizing_for_node(self, job, total_jobs, calibration):
        """``(node_id) -> JobSizing | None`` matching what the manifest will ask for.

        The queue's arithmetic and the manifest have to agree, or admission over- or
        under-fills a node by exactly the amount calibration changed. Both go through
        :func:`calibrated_resources`, so there is one rule rather than two that can drift.
        """
        if calibration is None:
            return None

        def _sizing(node_id):
            figures = self._node_figures(node_id)
            if not figures:
                return None
            return self._job_sizing(job, total_jobs, node_figures=figures)

        return _sizing

    def _refuse_a_bootstrap_that_did_not_hold(self, storage, bucket_name, prefix, job_name):
        """Fail the campaign when a bootstrap-sized run hit a limit it never chose.

        **Only for a campaign running on the bootstrap**, which is `sizing: calibrated`
        where calibration did not apply -- a pilot with no more jobs than nodes, or a
        cluster that can grow. Everywhere else the allocation was either declared by the
        author or measured on the node, and a run that hits it is reported and kept: that is
        `run_validity_view`'s job, and discarding such a run silently would be worse than
        labelling it.

        A bootstrap is different in kind. **Nobody chose it for this workload** -- it is a
        cluster-wide default that exists to get the first probe off the ground. A run that
        OOMs or throttles hard against it is not evidence about the stack; it is evidence
        that the default does not fit, and every further run of the campaign would carry the
        same fault. Continuing would spend the budget producing data whose allocation was
        never right, and the fault would surface later wearing the stack's clothes.

        Raises, so the campaign stops and the operator sees why. Best-effort on the read
        itself: a counter that cannot be fetched is not a verdict, and must never be the
        reason a campaign dies.
        """
        if not self._sizing_is_calibrated() or getattr(self, "_calibration_applies", True):
            return
        # Belt and braces on the same question: a campaign with measured figures is not on
        # the bootstrap, whatever any flag says.
        calibration = getattr(self, "_calibration", None)
        if calibration is not None and calibration.outcome().get("calibrated"):
            return
        from .node_calibration import (probe_refuse_ratio,  # noqa: PLC0415
                                       read_probe_measurement)

        index = getattr(self, "_job_index_by_name", {}).get(job_name)
        if index is None:
            return
        percentiles = self._container_percentiles()
        job_prefix = f"{prefix}_jobs/{self._job_artifact_path(index)}/"
        try:
            measured = read_probe_measurement(
                lambda k: storage.read_object(bucket_name, k), job_prefix,
                self._probe_container_files(), limits=self._probe_container_limits(),
                percentiles=percentiles)
        except Exception as exc:  # noqa: BLE001 - a counter we cannot read is not a verdict
            logger.debug("could not read counters for %s: %s", job_name, exc)
            return

        killed = sorted(n for n, st in (measured or {}).items()
                        if (st or {}).get("oom_kills", 0) > 0)
        # Judged against each container's OWN tolerance, like a probe is: a flat strict ratio
        # here would fail a campaign for throttling that the container's percentile absorbs
        # by construction, which is the reading the tolerance exists to prevent.
        capped = {n: st["throttled_ratio"] for n, st in (measured or {}).items()
                  if (st or {}).get("throttled_ratio", 0)
                  > probe_refuse_ratio(percentiles.get(n, 100.0))}
        if not killed and not capped:
            return
        what = []
        if killed:
            what.append(f"OOM-killed: {', '.join(killed)}")
        if capped:
            what.append("throttled against its own limit: "
                        + ", ".join(f"{k} {v:.1%}" for k, v in sorted(capped.items())))
        raise CampaignConfigError(
            f"{job_name} ran on the BOOTSTRAP allocation and {'; '.join(what)}. This "
            f"campaign asked for execution.sizing: calibrated, but calibration does not "
            f"apply to it -- a campaign with no more jobs than the cluster has nodes, or a "
            f"cluster that can grow -- so every container is running on the deployment's "
            f"default rather than a measured or declared figure. That default does not fit "
            f"this workload, and every remaining run would carry the same fault. Raise "
            f"ROBOVAST_BOOTSTRAP_CPU / ROBOVAST_BOOTSTRAP_MEMORY for the role named above, "
            f"or set execution.sizing: fixed and declare what this campaign needs.")

    def _collect_probes(self, storage, bucket_name, campaign_prefix) -> None:
        """Read whichever probes have finished, and let their nodes take work.

        Best-effort throughout: a probe that cannot be read leaves its node on the declared
        sizing, which is what a cluster with calibration off does anyway. Losing an
        optimisation must never cost the campaign.
        """
        from .node_calibration import (probe_completed, probe_output_dir,  # noqa: PLC0415
                                       read_probe_measurement, read_probe_tick_ratio)

        admission = self.admission
        if admission is None or not self._probes:
            return
        calibration = self._calibration
        if calibration is None:
            return
        # **Only probes the queue actually CREATED can be finished.** "Not among the jobs
        # still running" is true of a job that ended and equally of one that was never
        # created -- so a probe still waiting for room read as one that ran and produced no
        # verdict, and with a refusal now fatal that ended the campaign with a diagnosis
        # naming the wrong cause and a remedy that could not have helped. Seen on a node the
        # bootstrap pod does not fit, where the probe is unplaceable rather than broken.
        created = {name for name, state in admission.states(self._probe_owner()).items()
                   if state == _ADMIT_CREATED}
        outstanding = [k for k in self._probes if k in created]
        if not outstanding:
            return
        try:
            done = set(outstanding) - set(self.get_remaining_jobs(outstanding))
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            logger.debug("Batch %s: could not poll probes: %s", self._batch_tag, exc)
            return
        for key in done:
            node_id = self._probes.pop(key, None)
            admission.finished(key)
            if node_id is None:
                continue
            prefix = f"{campaign_prefix}{probe_output_dir(node_id)}/"
            try:
                measured = read_probe_measurement(
                    lambda k: storage.read_object(bucket_name, k), prefix,
                    self._probe_container_files(), limits=self._probe_container_limits(),
                    percentiles=self._container_percentiles())
            except Exception as exc:  # noqa: BLE001 - see docstring
                logger.warning("Batch %s: could not read probe for node %s: %s",
                               self._batch_tag, node_id, exc)
                measured = {}
            # The scenario's own verdict, not "did we read a file". The gate was handed
            # bool(measured) -- true of any probe that produced a CSV at all, which the
            # monitor writes whether or not the run got anywhere -- so it caught nothing.
            completed = probe_completed(lambda k: storage.read_object(bucket_name, k), prefix)
            tick = read_probe_tick_ratio(
                lambda k: storage.read_object(bucket_name, k), prefix)
            if not calibration.record(node_id, key, measured, completed=completed,
                                      percentiles=self._container_percentiles(),
                                      tick_ratio=tick):
                self._refuse_a_probe_that_could_not_measure(node_id, calibration)
                calibration.abandon(node_id, key)

    def _refuse_a_probe_that_could_not_measure(self, node_id, calibration) -> None:
        """Fail the campaign when a node's probe was refused.

        **A refused probe is not a node that merely stays unmeasured.** Its jobs would run at
        the seed while every calibrated node's ran at a measured figure, so the campaign
        silently mixes two sizings -- the inconsistency calibration exists to remove,
        reintroduced by the act of failing to measure. Continuing spends the whole budget
        producing runs that are not comparable with each other, and nothing in the results
        says so.

        Raised at the moment of refusal rather than at the end of the batch: the remaining
        runs would all carry the same fault, so the cheapest honest outcome is to stop.

        Not raised where calibration does not apply -- a pilot, or a cluster that can grow --
        because nothing was measured there by design and the campaign is not mixing anything.
        """
        if not getattr(self, "_calibration_applies", False):
            return
        reason = (calibration.outcome().get("refused") or {}).get(node_id)
        if not reason:
            return
        remedy = self._remedy_for(reason)
        raise CampaignConfigError(
            f"Node {node_id} could not be calibrated: {reason}. This campaign asked for "
            f"execution.sizing: calibrated, so its runs are meant to be sized from what each "
            f"node measures -- and a node that cannot be measured would run at the starting "
            f"allocation while the others run at measured figures, which is not a comparable "
            f"campaign. {remedy}, or set execution.sizing: fixed to declare the sizing "
            f"outright.")

    def _remedy_for(self, reason: str) -> str:
        """What would actually change the outcome, for the reason this probe was refused.

        The five reasons are not one fault with one fix, and a message that offers the same
        remedy to all of them sends the reader to edit something that has no bearing on what
        happened -- the "raise the allocation" advice reads especially wrong on a probe whose
        scenario never reached a verdict, where nothing about the allocation was in question.
        """
        declared = any(getattr(c, "resources", None) is not None
                       for c in (getattr(getattr(self, "plan", None), "containers", None) or ()))
        raise_it = ("Raise execution.containers.<name>.resources for the container named above"
                    if declared else
                    "Raise ROBOVAST_BOOTSTRAP_CPU / ROBOVAST_BOOTSTRAP_MEMORY for its role")
        if "throttled" in reason:
            # The measurement was of the ceiling, so the ceiling is what has to move.
            return raise_it
        if "OOM-killed" in reason:
            return ("Raise the memory for the container named above -- "
                    + ("execution.containers.<name>.resources.memory" if declared
                       else "ROBOVAST_BOOTSTRAP_MEMORY for its role"))
        if "no verdict" in reason:
            # Nothing to do with sizing: the probe runs a real trial, and that trial failed.
            return ("The probe runs one of this campaign's own configurations, so its "
                    "scenario failing is the campaign's to diagnose rather than the "
                    "allocation's -- read that node's probe log under _calibration/, and fix "
                    "what stopped the trial reaching a verdict")
        if "samples" in reason:
            return ("The trial finished too quickly to measure, so the figures would rest on "
                    "a handful of ticks -- lengthen the trial, or size this campaign yourself")
        return ("The probe produced nothing readable -- check that node's monitor output "
                "under _calibration/, and whether its runtime exposes the cgroup counters")

    def abandon_outstanding_probes(self) -> int:
        """Free every node this batch was still measuring. Returns how many.

        A probe that will never report -- because the batch is over, stopped, or failed -- has
        to release its node, or ``accepts_work`` answers False for that node for the rest of
        the campaign and the node is excluded from work it could have taken. Losing the
        measurement costs the declared sizing on that node, which is what a cluster with
        calibration switched off does anyway: a worse allocation, never a wrong result.

        Idempotent, and safe to call when there is no calibration at all.
        """
        calibration = self._calibration
        if calibration is None:
            self._probes.clear()
            return 0
        outstanding = list(self._probes.items())
        for key, node_id in outstanding:
            calibration.abandon(node_id, key)
        self._probes.clear()
        if outstanding:
            logger.info("Batch %s: abandoned %d calibration probe(s); their nodes stay on "
                        "the declared sizing", self._batch_tag, len(outstanding))
        return len(outstanding)

    def unmeasured_nodes(self) -> list:
        """Nodes this campaign held for measuring and never measured. Empty is the norm.

        **A node that ends a batch with no figures does not simply lose an optimisation.**
        It is held while its probe is outstanding, so it takes no work; when the probe is
        abandoned it becomes eligible again, is re-probed on the next batch, hits whatever
        stopped it the first time, and is held again. The campaign then runs to completion
        on the rest of the cluster and nothing in its results says a machine sat out.

        Reported rather than decided here: whether that ends the campaign is the caller's
        question, and this store does not know what a campaign is.
        """
        calibration = self._calibration
        if calibration is None or not getattr(self, "_calibration_applies", False):
            return []
        outcome = calibration.outcome()
        measured = set(outcome.get("calibrated") or [])
        return sorted(n for n in (self._probes.values() if self._probes else ())
                      if n not in measured)

    def _probe_container_limits(self) -> dict:
        """``{container: declared cpu ceiling}`` -- what each container could at most use.

        Handed to the measurement so a reading above a container's own quota is discarded as
        impossible rather than believed as a peak. Without it the probe's bring-up samples
        size a node from numbers no cgroup could produce.
        """
        from robovast.common.quantity import to_cores  # noqa: PLC0415

        limits = {}
        plan = getattr(self, "plan", None)
        for container in (getattr(plan, "containers", None) or ()):
            declared = resolve_resources(container.resources or {}, self.kube_context)
            ceiling = declared.get("cpu_limit") or declared.get("cpu")
            cores = to_cores(ceiling) if ceiling else None
            if cores:
                limits[container.name] = cores
                if container is getattr(plan, "main", None):
                    limits[MAIN_CONTAINER_NAME] = cores
        return limits

    def _calibration_settings(self, container) -> dict:
        """One container's resolved calibration: its own block over `.env` over the role rule.

        Resolved HERE because this is the layer that knows a container's role, and the role
        is what both defaults key on. The calibration store is handed the results rather than
        the rules -- it must not know what a container is for -- which is also why the same
        resolution feeds the probe's throttle tolerance and the allocation: a container
        cannot be judged against one statistic and then sized from another.
        """
        from .node_calibration import calibration_defaults  # noqa: PLC0415

        out = calibration_defaults(_bootstrap_role(getattr(container, "name", "") or "",
                                                   getattr(container, "roles", ()) or ()))
        declared = getattr(container, "calibration", None)
        if declared is None:
            return out
        for field in ("size_on", "limit"):
            value = getattr(declared, field, None)
            if value is not None:
                out[field] = value
        headroom = getattr(declared, "headroom", None)
        if headroom is not None:
            # Per field, so stating only `cpu` keeps the memory default rather than losing it.
            out["headroom"] = {
                **out["headroom"],
                **{f: getattr(headroom, f) for f in ("cpu", "memory")
                   if getattr(headroom, f, None) is not None}}
        return out

    def _calibration_by_container(self) -> dict:
        """``{container: resolved settings}`` for every container this job runs.

        The main container appears under BOTH the name the `.vast` gave it and
        :data:`MAIN_CONTAINER_NAME`, because the monitor writes its files under the latter --
        the same aliasing `_probe_container_limits` already applies, for the same reason.
        """
        out = {}
        plan = getattr(self, "plan", None)
        for container in (getattr(plan, "containers", None) or ()):
            name = getattr(container, "name", None)
            if not name:
                continue
            settings = self._calibration_settings(container)
            out[name] = settings
            if container is getattr(plan, "main", None):
                out[MAIN_CONTAINER_NAME] = settings
        return out

    def _container_percentiles(self) -> dict:
        """``{container: percentile}``, for the probe reader and the refusal tolerance."""
        return {name: float(settings.get("size_on") or 95.0)
                for name, settings in self._calibration_by_container().items()}

    def _probe_container_files(self) -> dict:
        """``{container: resource_usage file}`` for every container a job runs.

        The main container's file is named ``main`` rather than by its role, which is the
        same mismatch ``advice.py`` has to translate; the sidecars use their role names.
        """
        files = {MAIN_CONTAINER_NAME: "resource_usage_main.csv"}
        for sidecar in getattr(getattr(self, "plan", None), "sidecars", None) or []:
            name = getattr(sidecar, "name", None)
            if name:
                files[name] = f"resource_usage_{name}.csv"
        return files

    def _write_probe_param_files(self, transient_dir, jobs, scenario_name) -> list:
        """One ``<probe tag>.params.yaml`` per node. Returns the node ids written.

        Derived from the batch's FIRST job, so a probe runs a configuration the campaign
        actually contains rather than something invented for measuring. Its ``_output_dir`` is
        rewritten to the reserved probe directory, which is what keeps the scenario's results
        out of the run tree -- see ``probe_parameter_documents``.

        Silent no-op without an admission queue, or where calibration does not apply. The
        files are small, but writing them for a campaign that will never create a probe would
        leave a reader wondering which node ran what.
        """
        from .node_calibration import probe_parameter_documents  # noqa: PLC0415

        node_ids = self._probe_node_ids(len(jobs))
        if not node_ids or not jobs:
            return []
        base_docs = build_job_parameter_documents(jobs[0], scenario_name)
        for node_id in node_ids:
            docs = probe_parameter_documents(base_docs, node_id)
            with open(os.path.join(transient_dir, f"{probe_tag(node_id)}.params.yaml"),
                      "w", encoding="utf-8") as handle:
                handle.write(dump_multi_document_yaml(docs))
        return node_ids

    def _probe_node_ids(self, total_jobs: int) -> list:
        """Nodes this batch should measure before it places work on them.

        Empty when there is no queue to ask, when calibration does not apply to a campaign
        this size, or when every node is already calibrated -- the last being the ordinary
        case for every batch of a search after the first.
        """
        from .node_calibration import calibration_applies  # noqa: PLC0415
        from .node_calibration import NodeCalibration  # noqa: PLC0415

        admission = self.admission
        if admission is None:
            return []
        node_ids = admission.node_ids()
        calibration = admission.calibration(self.campaign, NodeCalibration)
        # Whether this campaign ASKED to be measured is `execution.sizing`, in its own
        # `.vast`. Whether the cluster and the campaign's shape make a probe worth running
        # is `calibration_applies`. Both have to hold.
        # **Decided once for the campaign, then kept.** `total_jobs` is this BATCH's count,
        # and the question is about the campaign's work against the cluster's nodes -- so
        # re-deciding per batch judges a long search by whichever batch happened to be
        # smallest, and a search that ramps its repetitions flips to "does not apply" while
        # its nodes stay measured. Everything downstream then reads that a campaign running
        # on measured figures is running on the bootstrap.
        if calibration.applies is None:
            calibration.applies = self._sizing_is_calibrated() and calibration_applies(
                total_jobs, len(node_ids), admission.growable())
        applies = calibration.applies
        # Mirrored onto the runner because a runner is per batch and reads it often; the
        # store holds the decision, this is a read of it.
        self._calibration_applies = applies
        if not applies:
            calibration.enabled = False
            return []
        return [n for n in node_ids if calibration.calibrated(n) is None]

    def _publish_capacity_wait(self, waiting: bool) -> None:
        """Tell the status whether this batch is queued, if anyone is listening.

        Best-effort and idempotent: a campaign driven without a control state (the local
        lane, a unit test) simply has nobody to tell, and reporting a queue must never be
        able to fail a batch.
        """
        if self._state is None:
            return
        try:
            self._state.update(waiting_for_capacity=waiting)
        except Exception:  # noqa: BLE001 - status reporting must not fail a batch
            logger.debug("Could not publish capacity wait for batch %s",
                         self._batch_tag, exc_info=True)

    def _warn_about_containers_with_no_role(self) -> None:
        """Name the containers calibration will treat as ad-hoc, in the campaign log.

        A role comes from the container's NAME -- `scenario`, `simulation`, `sut` -- and
        there is no way to declare one. A container named anything else therefore gets the
        ad-hoc bootstrap rather than its role's, and is sized on the SUSTAINED figure rather
        than the peak once measured.

        For a system under test called something else, both are wrong and only the first is
        loud: too small a bootstrap OOMs or throttles and stops the campaign, while sizing
        the thing under test on its sustained figure just lets it throttle mid-plan, which
        looks like the stack failing. So it is said up front, at INFO's louder neighbour, in
        the log the campaign keeps.

        Only under `calibrated`: with a declared figure the name decides nothing.
        """
        from robovast.common.config import CONTAINER_ROLES  # noqa: PLC0415

        plan = getattr(self, "plan", None)
        if plan is None or not self._sizing_is_calibrated():
            return
        adhoc = sorted(c.name for c in plan.containers
                       if not set(getattr(c, "roles", ()) or ()) & set(CONTAINER_ROLES))
        if not adhoc:
            return
        logger.warning(
            "execution.sizing is calibrated and %s %s not named after a role (%s), so each "
            "takes the ad-hoc bootstrap and is sized on its sustained use rather than its "
            "peak. If one of them is the system under test, rename it to '%s' -- the peak "
            "rule exists so the thing under test never throttles mid-plan, and it keys on "
            "the name.",
            ", ".join(adhoc), "is" if len(adhoc) == 1 else "are",
            "/".join(CONTAINER_ROLES), "sut")

    def _sizing_is_calibrated(self) -> bool:
        """Whether this campaign's reservations are measured rather than declared.

        Defaults to False for a runner with no campaign behind it -- the offline manifest
        emit and `vast prepare` -- so those keep refusing a pod that declares nothing, which
        is still the fault there: nothing is going to measure it.
        """
        return getattr(self, "sizing_mode", "fixed") == "calibrated"

    def _job_sizing(self, job, total_jobs, node_figures=None):
        """What one of this batch's pods asks the scheduler for, summed over its containers.

        Takes a *rendered* job manifest, not ``self.manifest``. That distinction cost a live
        run: the base manifest carries only the main container, because the sidecars -- where
        the simulator and the system under test live, and so nearly all of the request -- are
        appended per job in :meth:`_build_job_manifest`. Sizing from the base counted 1 core
        of a 4.75-core pod, and the queue admitted a whole batch at once.

        Every job of a batch is the same shape, so one rendering serves the batch.

        Native sidecars are included: Kubernetes adds a ``restartPolicy: Always`` init
        container's requests to the pod's effective total, and so does the scheduler.
        """
        from .kube_client import parse_resource  # noqa: PLC0415
        from .node_admission import JobSizing  # noqa: PLC0415

        spec = self.create_job_manifest(
            job, total_jobs, node_figures=node_figures)["spec"]["template"]["spec"]
        cpu = 0.0
        memory = 0
        gpu = 0
        containers = list(spec.get("containers") or [])
        containers += [c for c in (spec.get("initContainers") or [])
                       if c.get("restartPolicy") == "Always"]
        unsized = []
        for container in containers:
            requests = (container.get("resources") or {}).get("requests") or {}
            container_cpu = parse_resource(requests.get("cpu"))
            if container_cpu <= 0:
                unsized.append(container.get("name") or "<unnamed>")
            cpu += container_cpu
            memory += int(parse_resource(requests.get("memory")))
            gpu += int(parse_resource(requests.get(GPU_RESOURCE)))
        # A pod that asks for nothing "fits" every node, so the queue would admit the whole
        # plan in one pass -- the mass submission it exists to prevent, with no error
        # anywhere and preflight passing trivially. Refused at launch rather than paced into
        # a cluster that cannot hold it.
        #
        # Under `sizing: calibrated` a zero declaration is the NORMAL case rather than the
        # fault: the figure is measured per node and the bootstrap allocation is what the
        # first job asks for. What must still be refused is a pod that reaches admission
        # with no figure from either source, which is `_bootstrap_sizing` returning nothing.
        if cpu <= 0 and not self._sizing_is_calibrated():
            raise CampaignConfigError(
                "No container declares execution.containers.<name>.resources.cpu "
                f"({', '.join(unsized)}), so this campaign's pod would be admitted as "
                "needing zero cores and its whole plan created at once. Declare cpu (and "
                "memory) for the containers that do the work, or set "
                "execution.sizing: calibrated to have them measured per node.")
        if unsized and not self._sizing_is_calibrated():
            # Not fatal -- the queue still paces on what was declared -- but it paces on
            # less than the pod actually takes, so the cluster is oversubscribed by
            # whatever these use.
            logger.warning(
                "Campaign %s: %s declare no resources.cpu, so admission sizes this pod at "
                "%g cores and undercounts what it really takes. Declare cpu for every "
                "container.", self.campaign, ", ".join(unsized), cpu)
        return JobSizing(cpu=cpu, memory=memory, gpu=gpu)

    def get_remaining_jobs(self, job_names):
        """Which of *job_names* are still running. Same answer as before, one API call.

        **Only ever pass names that were actually created.** A name absent from the listing
        counts as finished -- which is right for a Job that was dropped or garbage-collected,
        and catastrophically wrong for one that has not been created yet: under admission
        every planned job would read as done and the batch would "finish" with zero results,
        silently. The caller keeps planned and created apart for exactly this reason.

        One ``list`` rather than a status read per name: at ``runs_per_job: 1`` a campaign has
        ~1435 jobs and this loop runs every two seconds, so the per-name version was the
        dominant API cost of a large batch.
        """
        wanted = set(job_names)
        if not wanted:
            return []
        label = f"jobgroup=scenario-runs,campaign-id={_label_safe_campaign(self.campaign)}"
        listing = self.k8s_batch_client.list_namespaced_job(namespace=self.namespace,
                                                            label_selector=label)
        by_name = {j.metadata.name: j for j in listing.items if j.metadata.name in wanted}
        running_jobs = []
        for job_name in job_names:
            job = by_name.get(job_name)
            if job is None:
                # Gone: finished and garbage-collected, or cleaned up. Either way not running.
                logger.debug("Job %s not in the listing; treating as finished.", job_name)
                continue
            status = job.status
            self._log_if_deadline_killed(job_name, status)
            if status.active is not None and status.active >= 1:
                running_jobs.append(job_name)
            elif status.completion_time is None and (status.failed is None or status.failed == 0):
                running_jobs.append(job_name)
        return running_jobs

    def _log_if_deadline_killed(self, job_name, status):
        """Emit a clear, greppable WARNING the first time *job_name* is seen to have
        been force-killed by ``activeDeadlineSeconds``.

        Kubernetes marks such a Job with a ``Failed`` condition whose reason is
        ``DeadlineExceeded``. These runs typically produce no ``/test.xml`` artifact,
        so this log line is the record that the job was hung and hard-stopped — for
        later analysis (grep ``HARD-KILLED by activeDeadlineSeconds``)."""
        if status is None or job_name in self._deadline_killed:
            return
        for cond in (status.conditions or []):
            if cond.type == "Failed" and cond.reason == "DeadlineExceeded":
                # ``_deadline_killed`` is a class-level frozenset for offline runners;
                # promote to an instance set before recording.
                if not isinstance(self._deadline_killed, set):
                    self._deadline_killed = set()
                self._deadline_killed.add(job_name)
                logger.warning(
                    "Campaign %s batch %s: job %s HARD-KILLED by activeDeadlineSeconds "
                    "(%ss) — likely hung, no self-shutdown.",
                    self.campaign, self._batch_tag, job_name, self._deadline_seconds)
                return

    def cleanup_jobs(self, campaign=None):
        """Delete jobs. If campaign is given, only delete jobs with that campaign-id label."""
        label_selector = "jobgroup=scenario-runs"
        if campaign is not None:
            label_safe = _label_safe_campaign(campaign)
            label_selector = f"jobgroup=scenario-runs,campaign-id={label_safe}"
        try:
            logger.debug(f"Deleting jobs with label selector '{label_selector}'")
            self.k8s_batch_client.delete_collection_namespaced_job(
                namespace=self.namespace,
                label_selector=label_selector,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0, propagation_policy="Background"
                ),
            )
            logger.info("Successfully deleted scenario-runs jobs")
        except client.rest.ApiException as e:
            logger.error(f"Error deleting jobs with label selector: {e}")

    def cleanup_pods(self, campaign=None):
        """Delete pods. If campaign is given, only delete pods with that campaign-id label."""
        label_selector = "jobgroup=scenario-runs"
        if campaign is not None:
            label_safe = _label_safe_campaign(campaign)
            label_selector = f"jobgroup=scenario-runs,campaign-id={label_safe}"
        try:
            logger.debug(f"Deleting pods with label selector '{label_selector}'")
            self.k8s_client.delete_collection_namespaced_pod(
                namespace=self.namespace,
                label_selector=label_selector,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0, propagation_policy="Background"
                ),
            )
            logger.debug("Successfully cleaned up scenario-runs pods")
        except client.rest.ApiException as e:
            logger.error(f"Error deleting pods with label selector: {e}")

    def get_job_manifest(self, image: str, resources: dict, env: list,
                         run_as_user: int = None) -> dict:
        """Generate the base Kubernetes job manifest from templates.

        Args:
            image: Image for the container the scenario runs in
            resources: Resource limits/requests for that container (cpu, memory)
            env: List of environment variables
            run_as_user: UID to run container as (defaults to 1000 if None)

        Returns:
            Dictionary containing the job manifest
        """
        if run_as_user is None:
            run_as_user = 1000

        # Normalize resources: may be a dict or a Pydantic model
        if hasattr(resources, 'cpu'):
            # `gpu` is carried through here too. Listing only cpu/memory silently dropped
            # a declared GPU for any model-shaped input, which is exactly the shape of
            # failure this whole path has to avoid: no error, just software rendering.
            resources = {'cpu': resources.cpu, 'memory': resources.memory,
                         'gpu': getattr(resources, 'gpu', None)}

        # Resolve per-cluster resource values for the active Kubernetes context
        resources = resolve_resources(resources, self.kube_context)

        self.env = env or []

        logger.debug(f"Using run_as_user={run_as_user} for job containers")

        yaml_str = JOB_TEMPLATE.format(image=image, namespace=self.namespace,
                                       pull_policy=pull_policy_for(image),
                                       compat_version=COMPAT_VERSION,
                                       min_compat_version=MIN_IMAGE_COMPAT)
        manifest = yaml.safe_load(yaml_str)

        # No queue-membership label, deliberately: this Job is admitted by RoboVAST's own
        # controller (node_admission.AdmissionController), which creates it only once the
        # cluster has room for it, so there is nothing left for an external queue to gate.
        # Ordering across concurrent campaigns is the controller's too, by campaign start
        # time, so no priority-class label carries it either.

        main_container = manifest['spec']['template']['spec']['containers'][0]
        main_container.setdefault('securityContext', {})['runAsUser'] = run_as_user

        stamp_resources(main_container, resources)
        # In the stepped shape the simulator IS this container, so this is where its GPU
        # goes; in the ROS shape the sidecar below carries it instead.
        main_env = main_container.setdefault('env', [])
        self._apply_gpu_to_container(
            main_container, main_env,
            self._gpu_request(resources, getattr(getattr(self, "plan", None), "main", None)))

        # Add custom environment variables
        if env:
            for env_var in env:
                if isinstance(env_var, dict):
                    for key, value in env_var.items():
                        main_container.setdefault('env', []).append({
                            'name': key,
                            'value': str(value)
                        })
        return manifest

    def _pin_image_refs(self, cache) -> None:
        """Resolve every image ref this campaign's pods run to the digest it names now.

        The root-cause half of the pull-storm fix (see :func:`pull_policy_for`), and a
        provenance fix in the same move. Robovast already recorded the digest -- but
        *after* a batch had run, read back off the pods (:meth:`_capture_image_digest`).
        Resolving it *before* the pods are written gets three things at once:

        * the kubelet stops re-contacting the registry for an image the node already has,
          because a digest ref takes ``IfNotPresent``;
        * every pod of the campaign provably runs the same bytes, instead of a floating
          tag that may be re-pushed between batch 1 and batch 50 -- a campaign whose
          system under test changes underneath it is not one experiment;
        * ``execution.yaml`` records what ran rather than what was asked for.

        *cache* is the backend's per-campaign dict, so a sweep asks the registry once for
        each distinct ref and not once per batch.

        **Fail-soft, deliberately.** An unreachable registry, a ref in a registry this
        deployment holds no credential for, a registry that omits the digest header: the
        ref is left exactly as it was, which is what would have run anyway. A campaign
        must not fail to start because an optimisation could not be applied -- and the
        unpinned ref then keeps ``Always``, which is correct for a name that may move.
        """
        if cache is None:
            cache = {}
        try:
            self._ensure_k8s_initialized()
        except Exception:  # noqa: BLE001 - offline manifest emit and tests reach no cluster
            logger.debug("no cluster to resolve image digests against", exc_info=True)
        sidecar = resolve_sidecar_image()
        refs = {c.image for c in self.plan.containers if c.image}
        refs.add(sidecar)
        if self.image:
            refs.add(self.image)
        for ref in sorted(refs):
            if ref in cache:
                continue
            cache[ref] = self._resolve_digest(ref)

        def pinned(ref):
            return cache.get(ref) or ref

        import dataclasses  # noqa: PLC0415 - only this method rebuilds the plan
        self.plan = dataclasses.replace(self.plan, containers=tuple(
            dataclasses.replace(c, image=pinned(c.image)) if c.image else c
            for c in self.plan.containers))
        self._sidecar_image = pinned(sidecar)
        self.image = pinned(self.image) if self.image else self.image
        moved = {ref: got for ref, got in cache.items() if got and got != ref}
        if moved:
            logger.info("Pinned %d image ref(s) to their digests for %s: %s",
                        len(moved), self.campaign,
                        ", ".join(f"{r} -> {d.rsplit('@', 1)[-1]}"
                                  for r, d in sorted(moved.items())))
        unpinned = sorted(ref for ref in refs if not cache.get(ref))
        if unpinned:
            logger.warning(
                "Could not resolve %d image ref(s) to a digest: %s. They keep their tag "
                "and imagePullPolicy 'Always', so every pod re-checks them with the "
                "registry -- which a wide batch can rate-limit itself out of. The pods "
                "also carry no guarantee of running the same bytes for the whole "
                "campaign.", len(unpinned), ", ".join(unpinned))

    def _resolve_digest(self, ref: str) -> str:
        """*ref* as ``repo@sha256:…`` if this deployment's registry will say, else ``""``.

        Uses the **pull** credential, because the question is what the kubelet will
        resolve the ref to, and the kubelet uses that one.
        """
        from .registry_client import manifest_digest  # noqa: PLC0415 - optional path
        try:
            registry = self.cluster_config.get_registry_config()
        except Exception:  # noqa: BLE001 - a registry is optional
            return ""
        try:
            return manifest_digest(
                ref, dockerconfigjson=self._registry_dockerconfig(registry),
                insecure=getattr(registry, "insecure", False),
                ca_path=self._registry_ca_path(registry))
        except Exception:  # noqa: BLE001 - never block a campaign on an optimisation
            logger.warning("could not resolve %s to a digest", ref, exc_info=True)
            return ""

    def _registry_dockerconfig(self, registry) -> str:
        """The pull Secret's ``.dockerconfigjson``, or ``""``. Never returned to a client."""
        import base64  # noqa: PLC0415
        name = getattr(registry, "pull_secret_name", "") or getattr(
            registry, "push_secret_name", "")
        if not name:
            return ""
        try:
            secret = self.k8s_client.read_namespaced_secret(name, self.namespace)
        except Exception:  # noqa: BLE001 - the probe is optional, the campaign is not
            return ""
        data = (secret.data or {}).get(".dockerconfigjson")
        if not data:
            return ""
        try:
            return base64.b64decode(data).decode()
        except (ValueError, UnicodeDecodeError):
            return ""

    def _registry_ca_path(self, registry) -> str:
        """The registry CA materialised to a file for ``requests``' ``verify=``, or ``""``.

        Cached on the instance: one file per runner, not one per ref.
        """
        name = getattr(registry, "ca_configmap_name", "")
        if not name:
            return ""
        if self._registry_ca_file is not None:
            return self._registry_ca_file
        self._registry_ca_file = ""
        try:
            cm = self.k8s_client.read_namespaced_config_map(name, self.namespace)
            pem = (cm.data or {}).get("ca.pem", "")
        except Exception:  # noqa: BLE001
            pem = ""
        if pem:
            fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 - lives for the process
                mode="w", suffix=".pem", prefix="robovast-registry-ca-", delete=False)
            fd.write(pem)
            fd.close()
            self._registry_ca_file = fd.name
        return self._registry_ca_file

    def _discover_gpu_support(self) -> None:
        """Record whether this cluster can schedule GPUs, and how they must be requested.

        Both answers come from the live cluster, so a campaign needs no per-cluster
        configuration to do the right thing: the same ``.vast`` renders on a GPU where one
        is advertised and in software where none is. A cluster that cannot answer is a
        cluster without GPUs as far as this run is concerned -- never an error, because a
        CPU-only cluster is the ordinary case and must behave exactly as it did before.
        """
        self._gpu_capacity = 0
        self._gpu_runtime_class = None
        try:
            from .kubernetes_gpu import get_cluster_allocatable_gpus, gpu_runtime_class_for
            self._gpu_capacity = get_cluster_allocatable_gpus(
                kube_context=self.kube_context)
            if self._gpu_capacity:
                # Asked rather than assumed, and "cannot tell" is not "no" -- see
                # gpu_runtime_class_for for why an unreadable answer still names the class.
                self._gpu_runtime_class = gpu_runtime_class_for(
                    kube_context=self.kube_context)
        except Exception as exc:  # noqa: BLE001 - absence of GPUs is not a failure
            logger.debug("Could not determine GPU support (%s); assuming none", exc)

    def _gpu_request(self, resources, container=None) -> int:
        """How many GPUs one container should request. **Opt-in: none unless declared.**

        ``resources.gpu`` is the whole answer. A campaign that renders asks for a device; one
        that does not gets none, and the cluster having a GPU is not taken as a reason to hand
        one out.

        Handing the simulator one automatically whenever the cluster advertises any -- so
        "use the GPU if there is one" needs no ``.vast`` edit -- buys nothing measurable. On a
        headless nav2 campaign that device does nothing: with ``gpu: 0`` the simulator's CPU is
        unchanged (mean 0.34 cores either way), trials take the same time (33.8 s against
        33.5 s), and the ``capture/`` the 3D run view replays is still written -- it is pose and
        geometry, not rendered frames. Nothing in such a world draws anything: no camera, and a
        lidar is a raycaster on the CPU. roqsim selects ``osmesa`` over ``egl`` by itself when no
        device is present (``roqsim.gl.select_offscreen_gl``), so there is nothing to fall back
        from.

        What it costs is concurrency, silently. A request is charged against the cluster's
        ``nvidia.com/gpu`` capacity at admission, and time-slicing replicas are a concurrency cap
        and not a VRAM budget (see :data:`DEFAULT_GPU_REPLICAS`) -- so one auto-claimed device
        per run caps a campaign that never renders a frame. Worse, it caps it *invisibly*: the
        default replica count is chosen to sit above the CPU ceiling, so the GPU only starts
        binding once someone right-sizes CPU, which is exactly when they are looking at CPU.

        A simulator that DOES render -- a camera or image sensor in the world, a video in the
        postprocessing -- declares ``resources: {gpu: 1}``, including on a CPU-only cluster,
        where the declaration stands and the pre-flight refuses the campaign rather than
        scheduling a job that would hang.
        """
        declared = (resources or {}).get('gpu')
        if declared is None:
            return 0
        try:
            return max(0, int(declared))
        except (TypeError, ValueError):
            logger.warning("Ignoring non-numeric resources.gpu %r", declared)
            return 0

    def _apply_gpu_to_container(self, spec, env_list, count) -> None:
        """Put *count* GPUs on one container spec, with the env the runtime needs."""
        if not count:
            return
        # Both requests and limits. Kubernetes defaults one from the other when a *Pod* is
        # created, but admission sizes a job from the Job's pod *template*, which no pod has
        # been created from yet -- so a request left empty is measured as zero GPUs and the
        # job is admitted onto a node with none.
        spec['resources'].setdefault('requests', {})[GPU_RESOURCE] = str(count)
        spec['resources'].setdefault('limits', {})[GPU_RESOURCE] = str(count)
        # NVIDIA_VISIBLE_DEVICES is deliberately NOT set, and the asymmetry with the
        # Compose lane (which sets it to `all`) is the point: there, nothing allocates
        # devices, so the container must claim them. Here the device plugin injects the
        # UUID it allocated into exactly the container that requested one, and overriding
        # that with `all` would hand every container every GPU regardless of quota.
        env_list.append({'name': 'NVIDIA_DRIVER_CAPABILITIES',
                         'value': GPU_DRIVER_CAPABILITIES})

    def _apply_pod_gpu_runtime(self) -> None:
        """Set ``runtimeClassName`` when any container of the plan wants a GPU.

        ``runtimeClassName`` is a pod field with no per-container form, so one container
        asking for a GPU decides it for the pod. On RKE2 it is also the only thing that
        makes the request usable: nvidia is a registered runtime rather than the default
        one, so without it the kubelet allocates the device and the container gets no
        driver, no ``/dev/dri`` and no way to render -- while the quota is still charged.
        """
        plan = getattr(self, "plan", None)
        if plan is None or not getattr(self, "_gpu_runtime_class", None):
            return
        wants = any(self._gpu_request(resolve_resources(c.resources, self.kube_context), c)
                    for c in plan.containers)
        if wants:
            self.manifest['spec']['template']['spec']['runtimeClassName'] = \
                self._gpu_runtime_class

    def gpu_resources_requested(self) -> bool:
        """Whether any container of this campaign requests a GPU.

        No longer feeds an admission pre-flight: the GPU count now travels in the
        ``JobSizing`` the controller admits against, so a request no node can satisfy is
        caught by ``preflight`` alongside cpu and memory rather than by a separate check
        that a queue's quota covered the resource at all.
        """
        plan = getattr(self, "plan", None)
        if plan is None:
            return False
        return any(self._gpu_request(resolve_resources(c.resources, self.kube_context), c)
                   for c in plan.containers)

    def _capture_image_digest(self, job_label: str) -> None:
        """Record the immutable digest the run pods actually used for the SUT image.

        Read once, from this batch's pods while they still exist (before Job cleanup),
        so ``execution.yaml`` can pin ``:latest`` to the exact ``repo@sha256:…`` the runs
        ran — and postprocessing reuses that identical image. Best-effort: any failure
        leaves the image unpinned (the tag), never blocking the campaign.
        """
        if self._resolved_image_digest:
            return  # already captured (search mode calls this per batch)
        # A ref pinned BEFORE the pods were written is already the digest those pods ran:
        # a digest ref cannot resolve to different bytes, so there is nothing to read back.
        # Taking it here is what up-front pinning promised ("execution.yaml records what ran
        # rather than what was asked for") and what this method did not do -- it read the
        # digest off the batch's pods instead, which is a race a SHORT batch loses: its pods
        # are reaped before the read, `image_revision` is written "unknown", and the search
        # loop's per-batch bag conversion can then resolve no execution image at all -- so
        # every batch fails to score and the campaign blames the world. The pod read below
        # still runs: it is the only source of a PER-CONTAINER digest.
        if self.image and "@sha256:" in self.image:
            self._resolved_image_digest = self.image
        try:
            pods = self.k8s_client.list_namespaced_pod(
                self.namespace, label_selector=job_label).items
            # Sidecars are NATIVE sidecars now, so they report under
            # init_container_statuses; reading only container_statuses would see the
            # scenario container alone and pin every role to its digest.
            statuses = [cs for pod in pods
                        for cs in ((pod.status.container_statuses or [])
                                   + (pod.status.init_container_statuses or []))]
            digest = resolve_image_digest(statuses, self.image)
            # One digest per container, so a consumer can ask for the image a PARTICULAR
            # role ran. The single `image_revision` is the scenario container's, which is
            # the wrong answer for anything the simulator produced: the run view compiles
            # a run's geometry from the world the capture names, and that world and its
            # exporter live in the simulation image. Keyed on the container's own digest,
            # it was compiled -- or rather, failed to compile -- in the scenario image.
            per_role = {}
            for cs in statuses:
                name = getattr(cs, "name", None)
                pullable = pullable_digest(getattr(cs, "image_id", None))
                if name and pullable and name not in per_role:
                    per_role[name] = pullable
        except Exception as exc:  # noqa: BLE001 - never block the run on a status read
            logger.debug("Could not resolve SUT image digest for %s: %s",
                         self.campaign, exc)
            return
        if per_role:
            self._resolved_image_digests = per_role
        if digest and not self._resolved_image_digest:
            self._resolved_image_digest = digest
            logger.info("Pinned SUT image for %s to %s", self.campaign, digest)

    # -- in-pod execution ---------------------------------------------------

    def _invalidate_restarted_jobs(self, job_label, job_names, jobs_by_name,
                                   campaign_root, storage, bucket_name,
                                   campaign_prefix) -> None:
        """Drop every job of this batch whose container crashed and was restarted.

        The response to a restart, and the whole of it. Each job is recorded in the
        intervention ledger, its evidence captured while the pod still exists, and then
        deleted -- ``get_remaining_jobs`` treats a gone Job as finished, so the batch drains
        around the hole exactly as it does for an operator's ``stop_job``. Its siblings
        finish, the cell scores over the samples it has left, and a cell that lost all of
        them degrades to ``no_sample``, which the search loop already handles.

        Order matters and is the same as ``stop_job``'s: record, publish, *then* delete. The
        pod dies asynchronously and takes its evidence with it.
        """
        try:
            restarted = restarted_job_forensics(self.k8s_client, self.namespace,
                                                job_label, job_names=job_names)
        except Exception as exc:  # noqa: BLE001 - probe failed this iteration
            logger.warning("Batch %s: could not check for restarted containers: %s",
                           self._batch_tag, exc)
            return
        for job_name, entry in sorted(restarted.items()):
            self._drop_job(job_name, entry["detail"], jobs_by_name=jobs_by_name,
                           campaign_root=campaign_root, storage=storage,
                           bucket_name=bucket_name, campaign_prefix=campaign_prefix,
                           forensics=entry)

    def _drop_blocked_jobs(self, expired, reasons_by_job, jobs_by_name, campaign_root,
                           storage, bucket_name, campaign_prefix) -> None:
        """Drop the jobs of this batch whose pod never started, once their grace is spent.

        The counterpart of :meth:`_invalidate_restarted_jobs` for the other way a job can
        fail to deliver a trial. A pod that cannot start has nothing to capture -- no
        container ever ran -- so this is that method without the forensics.

        The caller has already established that this is a *subset* of the batch (see the
        run loop); a whole batch that cannot start is a configuration fault and fails the
        campaign instead.
        """
        for job_name in sorted(expired):
            self._drop_job(job_name, f"pod never started -- {reasons_by_job[job_name]}",
                           jobs_by_name=jobs_by_name, campaign_root=campaign_root,
                           storage=storage, bucket_name=bucket_name,
                           campaign_prefix=campaign_prefix)

    def _drop_job(self, job_name, detail, *, jobs_by_name, campaign_root, storage,
                  bucket_name, campaign_prefix, forensics=None) -> None:
        """Discard one job of this batch, record why, and let the batch drain around it.

        The single response to a job that cannot deliver a usable trial, whichever way it
        got there: a container the kubelet restarted (its state is gone) or a pod that
        never started at all. The job is recorded in the intervention ledger, its evidence
        captured while the pod still exists, and then deleted -- ``get_remaining_jobs``
        treats a gone Job as finished, so the batch drains around the hole exactly as it
        does for an operator's ``stop_job``. Its siblings finish, the cell scores over the
        samples it has left, and a cell that lost all of them degrades to ``no_sample``,
        which the search loop already handles.

        Order matters and is the same as ``stop_job``'s: record, publish, *then* delete.
        The pod dies asynchronously and takes its evidence with it.

        *forensics* is the restart record whose container logs must be captured before the
        pod is collected; a job whose pod never started has none and passes ``None``.
        """
        if self._invalidated is None:
            self._invalidated = set()
        if job_name in self._invalidated:
            return
        self._invalidated.add(job_name)
        job = jobs_by_name.get(job_name)
        runs = tuple(f"{it.config_name}/{it.run_number}" for it in job.items) if job \
            else ()
        job_dir = f"_jobs/{self._job_artifact_path(job.index)}" if job else ""
        logger.warning(
            "Batch %s: invalidating job %s -- %s. Its %d run(s) are discarded; the "
            "rest of the batch continues.",
            self._batch_tag, job_name, detail, len(runs) or 1)
        # Evidence first, and never at the cost of the response: a diagnostic that
        # raises would turn the failure it documents into a different, worse one.
        if forensics is not None:
            try:
                self._capture_container_failures(forensics, job_name, job_dir, runs,
                                                 campaign_root, storage, bucket_name,
                                                 campaign_prefix)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Batch %s: could not capture evidence for %s: %s",
                               self._batch_tag, job_name, exc)
        try:
            record_intervention(
                Path(campaign_root), kind=KIND_INVALID, job_dir=job_dir,
                job_name=job_name, source="runner", detail=detail, runs=runs)
            self._publish_execution_file(storage, campaign_root, bucket_name,
                                         campaign_prefix, "interventions.json")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Batch %s: could not record the invalidation of %s: %s",
                           self._batch_tag, job_name, exc)
        try:
            self.k8s_batch_client.delete_namespaced_job(
                job_name, self.namespace, grace_period_seconds=0,
                propagation_policy="Background")
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

    def _capture_container_failures(self, entry, job_name, job_dir, runs, campaign_root,
                                    storage, bucket_name, campaign_prefix) -> None:
        """Write what the dead containers of *job_name* died of, before the pod is gone.

        The one artifact with a deadline. A restarted container's previous log lives only
        as long as the kubelet keeps its pod, and this method is called moments before the
        Job is deleted -- so this is the last point at which the question "why did it die?"
        can still be answered at all. The campaign that motivated this had it answered by a
        single formatted sentence, hours later, with the pod long collected.

        Published immediately rather than left for ``finalize_campaign``: search-mode
        postprocessing runs per batch, and a campaign that ends by being stopped never
        finalizes at all.
        """
        records = []
        for container in entry.get("containers") or ():
            record = dict(container)
            pod_name = record.get("pod_name")
            log_text, log_status = ("", "unavailable")
            if pod_name:
                log_text, log_status = previous_container_log(
                    self.k8s_client, self.namespace, pod_name, record["container"])
            record.update({
                "job_name": job_name,
                "job_dir": job_dir,
                "runs": list(runs),
                "batch": self._batch_tag,
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "log_status": log_status,
                "log_lines": len(log_text.splitlines()) if log_text else 0,
                "log_tail": log_text,
            })
            records.append(record)
        record_container_failures(Path(campaign_root), records)
        self._publish_execution_file(storage, campaign_root, bucket_name, campaign_prefix,
                                     "container_failures.json")

    def _publish_execution_file(self, storage, campaign_root, bucket_name,
                                campaign_prefix, filename) -> None:
        """Push one ``_execution/`` file to the object store now, best-effort.

        Best-effort because it is a mirror: the local copy is already written, and a
        transfer that fails must not cost the record it was copying.
        """
        path = Path(campaign_root) / "_execution" / filename
        if not path.is_file():
            return
        try:
            storage.upload_file(str(path), bucket_name,
                                f"{campaign_prefix}_execution/{filename}")
        except Exception as exc:  # noqa: BLE001 - a mirror, not the record
            logger.debug("Could not publish %s: %s", filename, exc)

    def run_batch_in_pod(self, campaign_root: str, whole_campaign: bool = False):
        """Upload, run and download one batch; this batch's results and the
        campaign-level snapshot (``_config``/``_transient``) land under *campaign_root*.

        ``whole_campaign`` is set in batch mode, where this batch *is* the entire
        campaign: the prefix holds no other batch's artifacts, so the results can be
        fetched with a single prefix download instead of the per-config enumeration
        that search mode needs to scope ``_jobs/`` to the current batch."""
        self._ensure_k8s_initialized()
        _, _, _, bucket_name, campaign_prefix = self._s3_settings()
        storage = in_pod_storage.storage_client_for(self.cluster_config)

        # 1. Prepare this batch's config tree + per-job parameter files.
        with tempfile.TemporaryDirectory(prefix="robovast_batch_") as out_dir:
            prepare_campaign_configs(
                out_dir, self.campaign_data, cluster=True,
                instance_type_command=_instance_type_command(self.cluster_config))
            self._write_job_param_files(out_dir, campaign_root)

            # 2. Upload to the batch's storage prefix (job init containers mirror from here).
            n = storage.upload_dir(out_dir, bucket_name, campaign_prefix)
            logger.info("Batch %s: uploaded %d config file(s) to %s/%s",
                        self._batch_tag, n, bucket_name, campaign_prefix)

        # 3. Build and submit one Job per packed job, then wait.
        # The up-front "can these jobs ever be admitted?" check is admission.preflight()
        # below: it asks whether the request fits any node's allocatable, which is the
        # question, asked of the cluster directly.
        jobs = self._build_jobs()
        total_jobs = len(jobs)
        # Derived rather than read back off a rendered manifest: under admission a job's
        # manifest is not built until there is room for it, but its name has to exist now --
        # the plan, the artifact paths and job_links all key on it, and they must agree.
        job_names = [_short_job_name(self.campaign, self._job_tag(job.index), job.index)
                     for job in jobs]
        # What the campaign root already holds. Empty for a campaign starting now -- so this
        # costs one `isfile` miss per job and changes nothing -- and, for one being re-entered
        # after its driver was taken away, the work that must not be run a second time. See
        # `_jobs_already_done`.
        done = self._jobs_already_done(jobs, campaign_root)
        if done:
            # Through the campaign's own logger, so this lands in its controller.log and
            # travels with the results: a campaign that lost part of a batch to a restart
            # is not the same artifact as one that ran clean, and it has to be able to say
            # so afterwards, not only while it is happening.
            logger.info("Batch %s: %d of %d job(s) already have every verdict; "
                        "adopting their results instead of re-running them.",
                        self._batch_tag, len(done), total_jobs)
            if self._state is not None:
                # And live, for whoever is watching the campaign right now.
                self._state.update(stage=f"resumed — adopted {len(done)} of {total_jobs} "
                                         f"job(s) finished before the restart")
        pending = [(job, name) for job, name in zip(jobs, job_names)
                   if job.index not in done]

        def _create_job(job, name, node_id=None):
            # The node's figures, or None while it is uncalibrated. **This is the same lookup
            # the queue made when it decided the job fits**, and it has to be: the queue
            # admits against JobSizing while Kubernetes reserves what the manifest says, so a
            # manifest built without them asks for the declared size on a node the queue
            # already counted as holding the smaller one -- over-admission, silently, on
            # exactly the nodes calibration was supposed to help.
            manifest = self.create_job_manifest(
                job, total_jobs, node_figures=self._node_figures(node_id))
            # Pinned to the node admission reserved room on, inside the operator's node
            # pool, so the placement and the reservation are one decision. See _pin.
            self._pin(manifest, node_id)
            try:
                self.k8s_batch_client.create_namespaced_job(namespace=self.namespace,
                                                            body=manifest)
            except client.exceptions.ApiException as exc:
                if exc.status == 409:
                    logger.debug("Batch %s: job %s already exists.", self._batch_tag, name)
                else:
                    raise

        admission = self.admission
        if not pending:
            # Every job this batch plans already has its results. Nothing to create and
            # nothing to queue -- and nothing to probe for either: calibration measures
            # nodes in order to place work on them, and there is none to place.
            created_names = []
            planned_count = 0
            logger.info("Batch %s: all %d job(s) already finished in an earlier life of "
                        "this campaign; waiting on nothing.", self._batch_tag, total_jobs)
        elif admission is None:
            # No queue: create everything at once, exactly as before. This is the path every
            # offline caller and every existing test takes.
            for job, name in pending:
                # Unpinned: without a queue nothing has reserved a node, so choosing one here
                # would be a guess the scheduler is better placed to make.
                _create_job(job, name)
            created_names = [name for _, name in pending]
            planned_count = 0
            logger.info("Batch %s: created %d of %d job(s); waiting for completion...",
                        self._batch_tag, len(created_names), len(job_names))
        else:
            from functools import partial  # noqa: PLC0415

            from .node_admission import AdmissionRefused, campaign_start_key  # noqa: PLC0415

            sizing = self._job_sizing(jobs[0], total_jobs)
            try:
                admission.preflight(sizing)
            except AdmissionRefused as exc:
                # Before a single job exists, because a request no node can hold otherwise
                # leaves the campaign waiting forever having created nothing -- and every
                # diagnosis path downstream reads pods, so none of them can see it.
                raise CampaignConfigError(str(exc)) from exc
            # Name -> index, kept because a finished job is reported by NAME and its
            # artifacts live under its INDEX. Recorded here, where the pairing already
            # exists, rather than re-derived from position later -- creation order varies
            # under admission and an index recovered by counting would be wrong.
            self._job_index_by_name = {n: j.index for j, n in zip(jobs, job_names)}
            calibration = self._start_probes(jobs, total_jobs, campaign_prefix)
            admission.submit(
                self.campaign,
                [(name, sizing, partial(_create_job, job, name))
                 for job, name in pending],
                started_at=campaign_start_key(self.campaign),
                sizing_for_node=self._sizing_for_node(jobs[0], total_jobs, calibration),
                accepts_node=(calibration.accepts_work if calibration else None))
            created_names = []
            planned_count = len(pending)
            logger.info("Batch %s: queued %d of %d job(s) for admission; creating as "
                        "room appears...", self._batch_tag, planned_count, len(job_names))
        # Job name -> its planned work, so a restart can be resolved to the runs it ruins
        # and to the artifact dir the ledger keys on. Built here and NOT read back from
        # ``_transient/job_links.yaml``: that manifest is downloaded after this loop
        # (`_write_job_links`), so on the first batch it does not exist yet.
        jobs_by_name = dict(zip(job_names, jobs))

        job_label = f"jobgroup=scenario-runs,campaign-id={_label_safe_campaign(self.campaign)}"
        # Per job, not per batch: two jobs can be blocked for different reasons, become
        # blocked at different moments, and deserve different tolerances. One shared
        # timer answered for all of them and so had to pick the shortest.
        blocked_since: "dict[str, float]" = {}
        last_blocked_log = 0.0
        last_refusal_log = 0.0
        while True:
            if self._state is not None and self._state.stop_requested:
                raise CampaignStopped(f"campaign {self.campaign} stopped during batch "
                                      f"{self._batch_tag}")
            if admission is not None:
                # **Probes first, then the drain.** A node is held while its probe is out and
                # freed the moment that probe reports -- per node, so one finishing does not
                # wait for the others. Draining first spent that freedom on the NEXT poll
                # instead of this one, leaving a measured node idle for a cycle for no reason.
                # Collecting first means a node calibrated in this pass takes work in this
                # pass.
                self._collect_probes(storage, bucket_name, campaign_prefix)
                # Works the GLOBAL queue, so this may create another campaign's jobs too --
                # that is what makes the ordering cluster-wide while keeping the queue
                # thread-free.
                admission.drain()
                states = admission.states(self.campaign)
                created_names = [n for n, st in states.items() if st == _ADMIT_CREATED]
                planned_count = sum(1 for st in states.values() if st == _ADMIT_PLANNED)
            remaining = self.get_remaining_jobs(created_names)
            if admission is not None:
                # Release the reservation of anything that has finished, so the capacity it
                # held is spendable again on the next drain.
                for name in set(created_names) - set(remaining):
                    admission.finished(name)
                    self._refuse_a_bootstrap_that_did_not_hold(
                        storage, bucket_name, campaign_prefix, name)
            if not remaining and not planned_count:
                # Cleared on the way out, not left to the next batch's first probe: between
                # those two moments the campaign is still in `running`, and a flag that
                # outlived its wait would suppress a verdict for a batch that is not queued
                # at all. Same failure `stage` had before a phase change learned to clear it.
                self._publish_capacity_wait(False)
                break
            # A Job whose pod can't start (bad/missing image, no pull creds, ...) stays
            # "active" with a Pending pod forever, so this loop would otherwise spin
            # indefinitely with no progress. Detect it and, once its grace window is
            # spent, fail the batch with Kubernetes' own message so the campaign reports
            # *why* instead of hanging. How long that window is depends on what the pod
            # is waiting for -- see below.
            try:
                blocked, contended = blocked_and_contended_reasons(
                    self.k8s_client, self.namespace, job_label)
                # The label selector is campaign-wide and finished Jobs linger for
                # ``ttlSecondsAfterFinished``, so scope the answer to THIS batch --
                # the same reason ``restarted_job_forensics`` takes ``job_names``.
                # Without it an earlier batch's job could be counted against this
                # one's tally, which is what decides config fault vs cluster.
                # CREATED names, not planned ones: a job that does not exist cannot be
                # blocked, and counting it in the whole-batch tally below would fail a
                # healthy campaign the moment its first job stalled while the rest were
                # still queued.
                blocked = {k: v for k, v in blocked.items() if k in set(created_names)}
                contended = {k: v for k, v in contended.items() if k in blocked}
            except Exception as exc:  # noqa: BLE001 - probe failed this iteration
                # Could not check pods this cycle. Treat as "unknown", NOT as
                # "nothing blocked": clearing blocked_since here would silently reset
                # the grace timer and let a truly blocked batch hang until the
                # deadline hard-kill. Keep any existing blocked state and retry.
                logger.warning("Batch %s: could not check for blocked jobs: %s",
                               self._batch_tag, exc)
                blocked, contended = None, {}
            # Publish whether this batch can run at all. A reader cannot judge a per-run
            # deadline while every job is queued for capacity, and only this loop knows.
            # Written every cycle, including the False case, so the flag never outlives the
            # wait that set it -- the failure `stage` had, where a marker true once was
            # still being reported long after.
            waiting = all_jobs_waiting_for_capacity(remaining, contended)
            if admission is not None and planned_count and not remaining:
                # A fact the queue holds, not something inferred from pods that do not exist.
                # Inferring it is what made a merely-queued campaign report as stalled.
                waiting = True
            self._publish_capacity_wait(waiting)
            if blocked:
                now = time.monotonic()
                reasons = "; ".join(sorted(set(blocked.values())))
                # Two tolerances, because "cannot start" covers two different futures.
                # A pod waiting its turn starts by itself: for a node, once the neighbour
                # holding the capacity finishes; for an image, once the pull the kubelet
                # is rate-limiting comes up its queue. Both appear when several campaigns
                # run at once and never when one does, and failing either on the
                # registry-blip timer threw away campaigns for the very conditions that
                # recover. Anything else here (an image that does not exist, a request no
                # node can hold) looks the same in ten minutes as in one, and still gets
                # the short timer.
                fresh = [job for job in blocked if job not in blocked_since]
                for job in fresh:
                    blocked_since[job] = now
                for job in [j for j in blocked_since if j not in blocked]:
                    del blocked_since[job]      # it started after all
                # Deleting a Job is asynchronous, so one already dropped keeps reporting
                # itself blocked for a poll or two; skipping it here keeps the timers and
                # the log honest without a second pass through `_drop_job`.
                dropped = self._invalidated or ()
                expired = [job for job, since in blocked_since.items()
                           if job not in dropped
                           and now - since >= (self._CONTENDED_GRACE_SECONDS
                                               if job in contended
                                               else self._BLOCKED_GRACE_SECONDS)]
                if fresh:
                    last_blocked_log = now
                    logger.warning(
                        "Batch %s: %d of %d job(s) cannot start%s: %s",
                        self._batch_tag, len(blocked), len(job_names),
                        "" if any(j not in contended for j in blocked)
                        else " yet (waiting their turn)", reasons)
                elif now - last_blocked_log >= self._BLOCKED_LOG_INTERVAL_SECONDS:
                    # Re-state it periodically: at 2s per iteration the "still running"
                    # line below otherwise buries a 15-minute wait under 450 lines that
                    # look like progress.
                    last_blocked_log = now
                    logger.warning("Batch %s: %d of %d job(s) still cannot start after "
                                   "%.0fs: %s", self._batch_tag, len(blocked),
                                   len(job_names), now - min(blocked_since.values()),
                                   reasons)
                if expired:
                    # The whole batch, or part of it — and that is the whole distinction.
                    # Every job of a batch runs the same images with the same reservation,
                    # so a cause that lives in the CONFIGURATION blocks all of them: a
                    # reference that names nothing, a credential the registry refuses, a
                    # reservation no node can hold. Such a campaign must still fail here,
                    # and fail fast, because no batch of it will ever run.
                    #
                    # A cause that blocks only SOME of them is, by that fact alone, not in
                    # the configuration — it is the cluster this batch happened to land in.
                    # Failing the campaign for it ends a long search mid-flight
                    # over two jobs of thirty-five, with eight hours of finished work
                    # behind it. So those jobs are dropped, exactly as a restarted one is,
                    # and the batch runs on with what is left.
                    if len(blocked) == len(job_names):
                        raise CampaignConfigError(
                            f"none of this batch's {len(job_names)} scenario job(s) could "
                            f"start — Kubernetes reports: {reasons}. Every job of a batch "
                            f"runs the same images with the same reservation, so a whole "
                            f"batch blocked points at the campaign rather than at the "
                            f"cluster: an image reason at the execution image reference "
                            f"and its pull credentials, an Unschedulable one at a "
                            f"reservation no node can satisfy (the message above names "
                            f"it).")
                    self._drop_blocked_jobs(expired, blocked, jobs_by_name, campaign_root,
                                            storage, bucket_name, campaign_prefix)
            elif blocked is not None:
                # A successful probe that found nothing blocked clears the timers.
                blocked_since.clear()
            # No grace period, deliberately: unlike a blocked pod, a restart has already
            # happened. The container lost its state, so every extra second spent waiting
            # buys a more convincing wrong answer rather than a chance of recovery.
            #
            # What is NOT deliberate is failing the campaign for it: one flaky sidecar in
            # one job of one batch would end a 50-batch search and orphan the batches that
            # had already finished. The trial is what the restart invalidates; the batch
            # around it is fine and the batches after it were never in question. So: drop
            # that job, record why, and keep going.
            self._invalidate_restarted_jobs(
                job_label, job_names, jobs_by_name, campaign_root,
                storage, bucket_name, campaign_prefix)
            # blocked is None (probe failed) => leave blocked_since unchanged.
            # Nothing suspends a Job, so the pod-based probe above is not blind to a
            # waiting job: a job that has not been created yet is PLANNED in the
            # controller, which _publish_capacity_wait reads directly rather than inferring
            # from a pod that does not exist.
            logger.info("Batch %s: %d/%d job(s) still running...",
                        self._batch_tag, len(remaining), len(job_names))
            if admission is not None and planned_count:
                # WHY nothing was created, not just that nothing was. The queue computes this
                # on every drain; without logging it an operator can see that a campaign is
                # waiting at "queued for capacity" and never what for. Rate-limited to
                # the blocked-log interval, because at 2s per iteration it would otherwise
                # repeat 450 times in a fifteen-minute wait.
                reason = admission.refusal(self.campaign)
                if reason and time.monotonic() - last_refusal_log >= \
                        self._BLOCKED_LOG_INTERVAL_SECONDS:
                    last_refusal_log = time.monotonic()
                    logger.info("Batch %s: %s", self._batch_tag, reason)
            time.sleep(2)
        # A stop that landed while the last jobs were being torn down leaves the loop
        # via the empty-remaining path; catch it here too before the result download.
        if self._state is not None and self._state.stop_requested:
            raise CampaignStopped(f"campaign {self.campaign} stopped during batch "
                                  f"{self._batch_tag}")
        # The loop breaks on an empty `remaining` BEFORE probing, so a restart in the last
        # job's last seconds is otherwise never seen. The pods are still here -- cleanup
        # runs at the end of this method -- so ask once more.
        self._invalidate_restarted_jobs(
            job_label, job_names, jobs_by_name, campaign_root,
            storage, bucket_name, campaign_prefix)
        if self._invalidated and len(job_names) > 1 and \
                len(self._invalidated) >= len(job_names):
            # Every job of a multi-job batch dropped is not a flake, it is a fault they
            # share -- a world file that does not exist, an image that cannot run here.
            # Carrying on would spend the rest of the budget producing cells with no
            # sample, so this is the case still worth ending the campaign for, and it is
            # the backstop for dropping jobs one at a time rather than failing the batch
            # around them: losing part of a batch is survivable, losing all of it is a
            # verdict. A single-job batch is exempt: one flake is 100% of one job.
            raise CampaignConfigError(
                f"every job in batch {self._batch_tag} ({len(job_names)}) was dropped -- a "
                f"container restarted, or a pod that never started. That is a fault they "
                f"share, not a flake. Evidence: _execution/interventions.json for what was "
                f"dropped and why; _execution/container_failures.json and the "
                f"container_failure table for the crashes among them.")
        logger.info("Batch %s: all jobs finished.", self._batch_tag)
        self._capture_image_digest(job_label)
        if self._deadline_killed:
            logger.warning(
                "Batch %s: %d job(s) hard-killed on the %ss deadline: %s",
                self._batch_tag, len(self._deadline_killed), self._deadline_seconds,
                ", ".join(sorted(self._deadline_killed)))

        # 4. Project this batch's results — and the campaign-level snapshot — from
        #    the object store into the campaign root, so the host campaign is
        #    complete (matching a local run and the service's fetch_campaign).
        os.makedirs(campaign_root, exist_ok=True)
        # A large batch's download is otherwise silent between "all jobs finished"
        # and the final "downloaded N" line — potentially minutes on hundreds of
        # files. Announce the start and log a running count every few seconds so
        # the campaign log shows progress instead of appearing hung.
        logger.info("Batch %s: downloading result files from object store...",
                    self._batch_tag)
        on_file = _download_progress_logger(self._batch_tag)
        # The 3s run-progress poller lists the whole campaign prefix over this same
        # (off-cluster) storage tunnel; pause it for the duration of the download so
        # the transfer runs uncontended. Resumed in the finally so a download error
        # (or an early return) can never leave the poller permanently off.
        got = 0
        if self._state is not None:
            self._state.suspend_progress()
        try:
            if whole_campaign:
                # Batch mode: this batch *is* the whole campaign, so the prefix holds
                # nothing but its own artifacts. One prefix download does a single
                # paginated list instead of one list per config — the per-config
                # enumeration below costs 600+ sequential list calls on a large batch,
                # during which the campaign sits in "running" with no progress.
                got = storage.download_prefix(bucket_name, campaign_prefix, campaign_root,
                                              on_file=on_file)
            else:
                # Search mode: the campaign prefix is flat/shared across batches, so we
                #    fetch by name: this batch's <config>/ dirs (self.configs == this
                #    batch's composed configs, the same names the controller scores at
                #    campaign_root/<config>/) and its job-artifact dir _jobs/<batch_tag>/
                #    (sysinfo.yaml, resource monitor, logs — read via each run's `job`
                #    symlink), plus the campaign-level _config/ (holds the .vast the
                #    auto-chain postprocessing reads) and _transient/. Batch-scoping
                #    _jobs avoids re-fetching prior batches each iteration; the small
                #    campaign-level dirs are re-fetched (idempotent — download_prefix
                #    never deletes, so the locally-accumulated _transient/job_links.yaml
                #    survives).
                job_root = f"_jobs/{self._batch_tag}" if self._batch_tag else "_jobs"
                targets = [c["name"] for c in self.configs if c.get("name")]
                targets += ["_config", "_transient", job_root]
                for rel in targets:
                    got += storage.download_prefix(
                        bucket_name, f"{campaign_prefix}{rel}",
                        os.path.join(campaign_root, rel), on_file=on_file)
        finally:
            if self._state is not None:
                self._state.resume_progress()
        logger.info("Batch %s: downloaded %d result file(s) into %s",
                    self._batch_tag, got, campaign_root)

        # 4b. Record this batch's <config>/<run>/job -> _jobs/<batch>/job-<idx>
        #     links, then materialise them as real symlinks now (not only at
        #     upload-to-share), so downstream readers resolve <run>/job/sysinfo.yaml
        #     during the driver's own metadata/postprocessing — as in a local run.
        self._write_job_links(campaign_root)
        create_job_links(campaign_root)

        # 5. Clean up this batch's jobs/pods (sequential batches share the
        #    campaign-id label, so only this batch's resources are present).
        self.cleanup_jobs(campaign=self.campaign)
        self.cleanup_pods(campaign=self.campaign)

    def _write_job_links(self, campaign_root: str):
        """Merge this batch's job-link entries into ``_transient/job_links.yaml``.

        ``<config>/<run>/job`` -> ``../../_jobs/<batch>/job-<idx>``. Accumulated
        across batches (the manifest is shared), uploaded by ``finalize_campaign``,
        and turned into real symlinks by the controller's upload-to-share
        compression.

        Run after the batch's results are downloaded, because the download can bring a
        manifest of its own. It accumulates through the same writer the upload side uses --
        one definition of "merge", so the two cannot drift into disagreeing about what the
        campaign's links are.
        """
        write_job_links_manifest(
            os.path.join(campaign_root, "_transient"), self._build_jobs(), self._batch_tag,
            base=read_job_links(campaign_root))


class KubernetesBackend(ExecutionBackend):
    """Run batches as Kubernetes Jobs from inside the controller pod.

    Args:
        cluster_config: Reconstructed cluster config (storage + scheduling).
        namespace: Kubernetes namespace for the jobs.
        kube_context: Host context name, used only to resolve per-cluster resource
            lists; the API client itself uses in-cluster config.
        log_tree: Forward ``-t`` (live scenario tree) to the jobs.
    """

    def __init__(self, *, cluster_config, namespace="default", kube_context=None,
                 log_tree=False, state=None, admission=None):
        # Owned by the service, not built here: one queue serves every campaign in the
        # process, and a backend built per campaign would give each its own -- which is
        # exactly the per-caller arbitration the queue exists to replace.
        self._admission = admission
        self.cluster_config = cluster_config
        self.namespace = namespace
        self.kube_context = kube_context
        self.log_tree = log_tree
        # Cooperative-stop signal (Ctrl+C / Stop): lets the batch wait loop abort
        # cleanly instead of pressing on into a doomed result download.
        self._state = state
        # Lazily-built read-only storage client for count_run_artifacts (the
        # controller's progress poller); separate from the write path.
        self._progress_storage = None
        # image ref -> the digest ref it resolved to, for this backend's lifetime. On the
        # backend rather than the runner because a search builds a fresh runner per batch,
        # and re-asking the registry fifty times answers a question that must not change
        # between batches anyway (see BatchJobRunner._pin_image_refs).
        self._image_digest_cache: dict = {}
        # node label -> that machine's facts, built once and reused. A campaign's runs
        # land on the same handful of machines thousands of times, and what a machine IS
        # cannot change between two runs of one campaign, so this is asked once per
        # campaign rather than once per job -- the same reasoning _discover_gpu_support
        # states for the GPU probe.
        self._node_facts_cache: dict | None = None

    def node_facts(self, label: str) -> dict | None:
        """The machine behind *label*, from this cluster's own nodes. See the base.

        Reads every node once, hashes each name the way the pod did, and answers from
        that map. Hashing our own view is what makes the lookup possible at all -- the
        label reaching us cannot be reversed -- and it means a label we do not recognise
        yields ``None`` rather than a guess.
        """
        if self._node_facts_cache is None:
            self._node_facts_cache = self._read_node_facts()
        return self._node_facts_cache.get(label)

    def _read_node_facts(self) -> dict:
        """``{node label: facts}`` for every node this cluster has, or ``{}``.

        Never raises: provenance is worth having and not worth failing a campaign for, so
        an unreachable API or a missing permission records the machines with no facts
        rather than ending the run.
        """
        try:
            self._init_k8s_clients()
            v1 = client.CoreV1Api(self.k8s_api_client)
            nodes = (v1.list_node().items or [])
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Could not read node facts: %s", exc)
            return {}
        facts = {}
        for node in nodes:
            name = getattr(getattr(node, "metadata", None), "name", None)
            label = node_label(name)
            if not label:
                continue
            status = getattr(node, "status", None)
            info = getattr(status, "node_info", None)
            facts[label] = {
                "capacity": dict(getattr(status, "capacity", None) or {}),
                "allocatable": dict(getattr(status, "allocatable", None) or {}),
                # machineID and systemUUID are deliberately absent: they identify the
                # hardware, and shipping them beside a label whose whole purpose is that
                # it does not would hand back what hashing the name took away.
                "node_info": {k: v for k, v in vars(info).items()
                              if v is not None and not k.startswith("_")
                              and k not in ("machine_id", "system_uuid", "boot_id")}
                             if info is not None else {},
                # kubernetes.io/hostname restates the node's name, so it would put the
                # hostname back into the record by value.
                "labels": {k: v for k, v in
                           (getattr(getattr(node, "metadata", None), "labels", None)
                            or {}).items() if k != "kubernetes.io/hostname"},
            }
        return facts

    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions, whole_campaign: bool = False) -> None:
        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        execution_params = campaign_data.get("execution", {}) or {}
        from robovast.execution.backends import _scenario_image
        image = _scenario_image(execution_params, options)
        runner = BatchJobRunner.for_batch(
            campaign_data=campaign_data,
            campaign_id=campaign_id,
            batch_tag=batch_tag,
            runs=runs,
            cluster_config=self.cluster_config,
            namespace=self.namespace,
            image=image,
            kube_context=self.kube_context,
            log_tree=self.log_tree or options.log_tree,
            state=self._state,
            built_images=options.images,
            image_digest_cache=self._image_digest_cache,
            admission=self._admission,
        )
        batch_error = None
        try:
            runner.run_batch_in_pod(campaign_root, whole_campaign=whole_campaign)
        except BaseException as exc:  # noqa: BLE001 - recorded, never swallowed
            # Remembered only so the cleanup below can tell a batch that ended from one that
            # is unwinding. Re-raised untouched at the end of the `finally`.
            batch_error = exc
            raise
        finally:
            # Every exit, not just the happy one. A batch that raises on its way out --
            # stopped, a config error, anything unexpected -- would otherwise leave its
            # reservations held for the life of the process, shrinking every other campaign's
            # usable capacity invisibly and cumulatively. Here rather than inside the runner
            # because the backend owns the queue, and a search builds a fresh runner per batch.
            if self._admission is not None:
                dropped = self._admission.cancel(campaign_id)
                # **The probes are a second owner and were cancelled by nothing.** They queue
                # under `<campaign>#probes` so they stay out of the campaign's own progress
                # counts, and `cancel` matches an owner exactly -- so a probe still PLANNED
                # when this batch ended stayed in the GLOBAL queue for the life of the
                # process, at priority 1, to be created later by some other campaign's drain
                # through a partial bound to this dead runner. One that had reached CREATED
                # held its node's capacity just as permanently. And because the runner's
                # calibration still listed it as outstanding, `accepts_work` answered False
                # for that node for every later batch of this campaign -- so the node was
                # excluded from the campaign entirely, which is the opposite of what
                # measuring it was for.
                #
                # The batch loop can reach here with a probe outstanding without anything
                # going wrong: the loop exits on the CAMPAIGN's jobs being done, and a probe
                # pinned to a node another campaign has filled may never have been created at
                # all. A created probe at least dies on its activeDeadlineSeconds; one that
                # was never created has no timer of any kind.
                dropped += self._admission.cancel(
                    f"{campaign_id}{BatchJobRunner._PROBE_OWNER_SUFFIX}")
                # Asked BEFORE abandoning, which is what clears the record of who was still
                # being measured. Only when the batch is ending normally: during a stop or a
                # failure an outstanding probe is expected, and raising here would replace
                # the reason the campaign is unwinding with a consequence of it.
                unmeasured = [] if batch_error else runner.unmeasured_nodes()
                runner.abandon_outstanding_probes()
                if unmeasured:
                    raise CampaignConfigError(
                        f"{', '.join(unmeasured)} could not be measured: their probes never "
                        f"ran, so those machines took no work and the campaign would have "
                        f"finished on the rest of the cluster without saying so. A node held "
                        f"for measuring is re-probed on the next batch, meets whatever "
                        f"stopped it before, and is held again -- so this does not resolve "
                        f"itself. The usual cause is a probe larger than what the node can "
                        f"spare: the bootstrap pod is the three roles summed "
                        f"(ROBOVAST_BOOTSTRAP_CPU / _MEMORY), and a campaign that declares "
                        f"execution.containers.<name>.resources sizes its probe from those "
                        f"instead.")
                if dropped:
                    logger.info("Batch %s: released %d job(s) that were never created",
                                batch_tag, dropped)

        # Record _execution/execution.yaml now (not at finalize), so the campaign
        # root is complete before the controller chains analysis postprocessing —
        # which reads the execution image from it. This mirrors the local backend,
        # whose run.sh writes execution.yaml during the run. Best-effort cluster
        # info; degrades in-pod. Idempotent across a search's repeated batches.
        from robovast.common.execution import \
            create_execution_yaml  # pylint: disable=import-outside-toplevel
        create_execution_yaml(runs, campaign_root,
                              execution_params=execution_params,
                              context=self.kube_context,
                              image_digest=getattr(runner, "_resolved_image_digest", None),
                              image_digests=getattr(runner, "_resolved_image_digests", None))

    def publish_records(self, campaign_root: str) -> None:
        """Publish ``campaign.db`` alone, so an unfinished campaign still has its records.

        The one file, not the directory: this runs before any compute is spent and again
        at every batch boundary, and the campaign root beside it holds the batch's results.
        ``finalize_campaign`` is what publishes those, once.

        Best-effort. The campaign is mid-flight and its own result uploads go through the
        same client moments later, so a store that is genuinely unreachable is reported by
        those with a real error rather than by ending the campaign over its bookkeeping.
        """
        db = os.path.join(campaign_root, "campaign.db")
        if not os.path.isfile(db):
            # Nothing to publish yet is not a failure: the local batch runner reaches the
            # per-batch call before the store has been created in some test lanes.
            return
        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        try:
            bucket, prefix = in_pod_storage.campaign_storage_location(
                self.cluster_config, campaign_id)
            storage = in_pod_storage.storage_client_for(self.cluster_config)
            storage.upload_file(db, bucket, f"{prefix}campaign.db")
        except Exception as e:  # noqa: BLE001 - bookkeeping must not end a campaign
            logger.warning("Could not publish campaign.db for %s: %s", campaign_id, e)

    def finalize_campaign(self, campaign_root: str) -> None:
        """Publish the canonical campaign to storage so the bucket matches local.

        Jobs upload raw per-run results (``<config>/<run>/test.xml`` etc.) and
        ``_jobs/`` *before* the controller runs search postprocessing, and each
        batch uploads ``_config``/``_transient`` and records
        ``_execution/execution.yaml``. This step publishes the full in-pod
        ``campaign_root`` — which additionally holds ``campaign.db`` and the
        postprocessing-derived per-run artifacts (e.g. ``metrics.csv``, written next
        to ``trajectory.csv`` by ``QuadMetrics``) — so ``upload-to-share`` +
        ``download`` yield a layout identical to a local run. Re-uploading the small
        raw files is idempotent.
        """
        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        # The campaign is over, so its per-node figures are too. Deliberately not reused by
        # the next campaign -- they were measured under this one's contention, for this one's
        # containers -- and this is the only campaign-level hook the backend has; the batch's
        # `finally` releases reservations but must NOT touch calibration, or a search re-probes
        # every node every batch.
        if self._admission is not None:
            self._admission.forget_calibration(campaign_id)
        bucket, prefix = in_pod_storage.campaign_storage_location(
            self.cluster_config, campaign_id)
        storage = in_pod_storage.storage_client_for(self.cluster_config)

        n = storage.upload_dir(campaign_root, bucket, prefix)
        logger.info("Published canonical campaign (%d file(s), incl. campaign.db / "
                    "_execution / metrics) to %s/%s", n, bucket, prefix)

    def preflight_upload_to_share(self) -> None:
        """Fail fast when ``--upload-to-share`` is set but no share is configured.

        The upload runs in-driver (this same process/env), so the env checked here is
        exactly the one :meth:`share_campaign` would read at the finish tail. Raising
        now — before the campaign runs — turns a silent, end-of-run skip into an
        up-front, actionable error.
        """
        from . import in_pod_upload  # pylint: disable=import-outside-toplevel

        if not in_pod_upload.share_type_configured():
            raise CampaignConfigError(
                "--upload-to-share was requested, but no share provider is configured: "
                "ROBOVAST_SHARE_TYPE is unset in the campaign environment.\n"
                "Set it (and its provider credentials) in the environment / .env that "
                "'vast serve' runs with — e.g.\n"
                "  ROBOVAST_SHARE_TYPE=gcs\n"
                "  ROBOVAST_GCS_BUCKET=my-robovast-results\n"
                "— or drop --upload-to-share.")

    def share_campaign(self, campaign_root: str, options,
                       progress_callback=None) -> None:
        """Stream the campaign straight to the configured share provider.

        Overrides the local tar.gz-on-disk behaviour: the campaign is already on the
        driver's scratch (the batch runner downloaded it back for scoring), so it is
        tarred + gzipped **on the fly** into the provider's request body — no
        compressed copy ever lands on disk, which matters for ~1TB campaigns. At
        campaign end this runs before analysis postprocessing, so what goes up is the
        minimal raw snapshot; the name says which it is either way. A share failure is
        surfaced but never loses the campaign (the controller wraps this call).
        """
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        from robovast.execution.share_providers.naming import (  # pylint: disable=import-outside-toplevel
            archive_name, campaign_variant)

        from . import in_pod_upload  # pylint: disable=import-outside-toplevel


        campaign_id = os.path.basename(os.path.normpath(campaign_root))
        provider = in_pod_upload.load_provider_from_env()
        if provider is None:
            # Unreachable in normal flow: preflight_upload_to_share() already rejected
            # this at campaign start. Kept as a loud backstop rather than a silent skip
            # so a share going unconfigured can never pass unnoticed again.
            raise CampaignConfigError(
                "upload-to-share enabled but no share provider is configured "
                "(ROBOVAST_SHARE_TYPE unset) for %s." % campaign_id)
        in_pod_upload.verify_share_access(provider)
        variant = campaign_variant(campaign_root)
        object_name = archive_name(campaign_id, variant)
        logger.info("Streaming %s campaign %s to %s share as %s...",
                    variant, campaign_id, provider.SHARE_TYPE, object_name)
        # The denominator for the progress bar. Compression means the request body's
        # length stays unknown to the last byte, so what is counted is the payload going
        # *in*; a metadata walk is cheap next to the upload that reads all of it anyway.
        on_member = getattr(progress_callback, "on_member", None)
        if on_member is not None:
            progress_callback.set_source_total(
                campaign_archive.campaign_source_bytes(campaign_root))
        with campaign_archive.campaign_tar_stream(campaign_root,
                                                  on_member=on_member) as stream:
            provider.upload_archive_stream(stream, object_name,
                                           progress_callback=progress_callback)
        if on_member is not None:
            progress_callback.finish()
        logger.info("Uploaded %s to the %s share.", object_name, provider.SHARE_TYPE)

    def count_run_artifacts(self, campaign_id: str,
                            campaign_root: str) -> int | None:
        """Count the per-run JUnit reports uploaded under the campaign's prefix.

        The object-store counterpart of :meth:`DockerBackend.count_run_artifacts`:
        each finished run uploads its own ``test.xml``, so counting them under the
        (flat, campaign-wide) prefix gives cumulative finished runs. The local
        ``campaign_root`` is not the source of truth here — results reach it only when
        a batch is downloaded — so it is unused.
        """
        del campaign_root
        bucket, prefix = in_pod_storage.campaign_storage_location(
            self.cluster_config, campaign_id)
        if self._progress_storage is None:
            self._progress_storage = in_pod_storage.storage_client_for(self.cluster_config)
        keys = self._progress_storage.list_keys(bucket, prefix)
        return sum(1 for k in keys if k.endswith(f"/{self.RUN_SENTINEL}"))
