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

"""A campaign's postprocessing split across Jobs: one per part of its runs, then one more.

The steps that may run on a part of the campaign -- the *map*, the longest run-scoped
prefix of its postprocessing (``postprocessing.split_postprocessing``) -- run in one Job per
part, each staging only its part's runs. The admission queue places them wherever the
cluster has room, so the campaign is postprocessed by as many pods as the operator allows
(:data:`MAX_PARALLEL_ENV`) instead of by one. The rest -- the *reduce*: the campaign-scoped
steps, the index ingest, the provenance record -- then runs in one Job over the whole tree,
exactly as an unsplit postprocess does, with the map steps skipped.

A part is a set of whole scenario jobs with their runs, never part of one: a job's
infrastructure bag and its timeline are shared by its runs, so two parts holding runs of
one job would convert that bag twice and deliver the same file at once.
"""

import dataclasses
import logging
import os
import time
from functools import partial
from typing import Dict, List

from . import pod_access
from .admitted_jobs import AdmittedJobs, running_jobs
from .campaign_job import pin_campaign_job
from .kube_client import api_transport_errors

logger = logging.getLogger(__name__)

#: A cap on how many Jobs one campaign's postprocessing is split into, for an operator who
#: wants one. Set in the ``.env``; ``vast cluster setup`` and ``vast service upgrade`` carry
#: it into the service Deployment. ``1`` keeps the whole postprocessing in one Job.
#:
#: **Unset is not "off".** The split then takes the cluster, the way a campaign's runs do:
#: as many parts as the cluster could run at once (:func:`max_parallel`). What actually runs
#: at once is the admission queue's decision either way -- this only decides how many Jobs
#: the work is cut into.
MAX_PARALLEL_ENV = "ROBOVAST_POSTPROCESS_MAX_PARALLEL"

#: How many parts to cut a campaign into when the cluster will not say how large it is. Not
#: one: a deployment that cannot answer is the one whose operator is least likely to have
#: set a cap, and leaving it unsplit would be the silent "off" this default exists to avoid.
PARTS_WITHOUT_A_KNOWN_CLUSTER = 8

#: Part names, ``part-1``, ``part-2``, ...: the Job discriminator, the staging selection
#: and the prefix of every file a part writes for itself. Counted from one, because these
#: names are read by people -- in the campaign log, in a Job listing and in the campaign's
#: own directory.
PART_PREFIX = "part-"


def max_parallel(raw=None, convert_cpu=None) -> int:
    """How many parts a campaign's postprocessing may be cut into.

    The operator's cap (:data:`MAX_PARALLEL_ENV`) when one is set. Unset, as many parts as
    the cluster could run at once: its recorded size (``ROBOVAST_CLUSTER_MAX_CPU``, written
    at setup) divided by the CPU one conversion asks for, which is the campaign's own
    ``results_processing.resources``. So a deployment that sets nothing postprocesses with
    the cluster it has, exactly as a campaign's runs do, and a cluster that cannot say how
    large it is gets :data:`PARTS_WITHOUT_A_KNOWN_CLUSTER`.

    Cutting the work into more parts than fit buys nothing: the queue creates a part's Job
    only when there is room for it, so the extra parts would wait their turn having each
    paid for a stage of their own.

    Raises ``ValueError`` naming the variable for anything but a positive integer: a cap
    that silently fell back would split, or not split, a deployment against its operator's
    word.
    """
    raw = os.environ.get(MAX_PARALLEL_ENV, "") if raw is None else raw
    raw = str(raw).strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value < 1:
            raise ValueError(f"{MAX_PARALLEL_ENV} must be a whole number of Jobs, 1 or more "
                             f"(1 postprocesses a campaign in one Job); it is {raw!r}")
        return value
    return _parts_that_fit(convert_cpu)


def _parts_that_fit(convert_cpu=None) -> int:
    """As many parts as the cluster could run at once, for a conversion of *convert_cpu*."""
    from robovast.common.quantity import to_cores  # noqa: PLC0415

    from .cluster_capacity import recorded_maximum  # noqa: PLC0415

    if convert_cpu is None:
        from robovast.results_processing.postprocessing import (  # noqa: PLC0415
            POSTPROCESS_CONVERT_DEFAULTS)
        convert_cpu = POSTPROCESS_CONVERT_DEFAULTS["cpu"]
    try:
        cluster_cpu, _memory = recorded_maximum()
    except ValueError:
        # The figure is the cluster's business, not this campaign's: an unreadable one is
        # reported where it is configured, and the postprocess uses the fallback.
        logger.warning("could not read the cluster's recorded size; postprocessing in %d "
                       "part(s)", PARTS_WITHOUT_A_KNOWN_CLUSTER)
        cluster_cpu = None
    per_part = to_cores(convert_cpu) or 1
    if not cluster_cpu:
        return PARTS_WITHOUT_A_KNOWN_CLUSTER
    return max(1, int(cluster_cpu // per_part))


@dataclasses.dataclass
class Part:
    """One part's runs (``config/run``) and the scenario jobs they ran in (``_jobs/...``)."""

    name: str
    runs: List[str] = dataclasses.field(default_factory=list)
    jobs: List[str] = dataclasses.field(default_factory=list)
    bytes: int = 0


def plan_parts(campaign_root: str, limit: int) -> List[Part]:
    """Split the campaign's runs into at most *limit* parts of about equal size.

    Returns ``[]`` when there is nothing to split -- a cap of one, or fewer than two units of
    work -- which is the caller's cue to postprocess in one Job.

    The unit is a scenario job with every run it served (see the module docstring); a run
    with no job of its own is a unit by itself. Units are placed largest first onto the
    lightest part, which keeps the parts within one unit of each other.

    A unit weighs its rosbags: they are what the work scales with, and postprocessing never
    changes them, so planning again over a campaign some parts already delivered into
    gives the same parts -- which is what lets a resumed postprocess wait for the Jobs an
    earlier attempt left running.
    """
    from robovast.common.campaign_data import list_config_dirs, list_run_dirs  # noqa: PLC0415
    from robovast.common.execution import job_artifact_dir, read_job_links  # noqa: PLC0415

    if limit < 2:
        return []
    links = read_job_links(campaign_root)
    units: Dict[str, dict] = {}
    for config_dir in list_config_dirs(campaign_root):
        for run_dir in list_run_dirs(config_dir):
            run = f"{config_dir.name}/{run_dir.name}"
            try:
                job = os.path.relpath(job_artifact_dir(campaign_root, run, links=links),
                                      campaign_root).replace(os.sep, "/")
            except FileNotFoundError:
                job = ""
            key = job or f"run:{run}"
            if key not in units:
                units[key] = {"runs": [], "job": job,
                              "bytes": _bag_bytes(os.path.join(campaign_root, job))
                              if job else 0}
            units[key]["runs"].append(run)
            units[key]["bytes"] += _bag_bytes(str(run_dir))
    if len(units) < 2:
        return []
    parts = [Part(name=f"{PART_PREFIX}{i + 1}") for i in range(min(limit, len(units)))]
    for unit in sorted(units.values(), key=lambda u: (-u["bytes"], u["runs"][0])):
        lightest = min(parts, key=lambda s: (s.bytes, s.name))
        lightest.runs.extend(unit["runs"])
        if unit["job"]:
            lightest.jobs.append(unit["job"])
        lightest.bytes += unit["bytes"]
    return [s for s in parts if s.runs]


def _bag_bytes(path: str) -> int:
    """Bytes of the rosbags under *path*."""
    from robovast.execution.campaign_archive import BAG_DIR_NAMES  # noqa: PLC0415

    total = 0
    for root, _dirs, names in os.walk(path):
        if not any(part in BAG_DIR_NAMES for part in os.path.relpath(root, path).split(os.sep)):
            continue
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


#: Beside the parts' membership: what a split postprocess was asked for, so a service that
#: restarts while its parts run can resume it as it was started (:func:`read_plan`).
PLAN_FILE = "plan.json"


def write_plan(campaign_root: str, parts, *, force: bool, skip) -> None:
    """Record the split's parts and the options it runs with."""
    import json  # noqa: PLC0415

    from robovast.execution.campaign_archive import PARTS_DIR  # noqa: PLC0415

    path = os.path.join(campaign_root, *PARTS_DIR.split("/"), PLAN_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"parts": [s.name for s in parts], "force": bool(force),
                   "skip": sorted(set(skip or ()))}, f)


def read_plan(campaign_root: str):
    """The recorded split (:func:`write_plan`), or ``None`` when there is none."""
    import json  # noqa: PLC0415

    from robovast.execution.campaign_archive import PARTS_DIR  # noqa: PLC0415

    try:
        with open(os.path.join(campaign_root, *PARTS_DIR.split("/"), PLAN_FILE),
                  encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError):
        return None
    return plan if isinstance(plan, dict) and plan.get("parts") else None


def part_job_names(campaign_id: str, parts) -> List[str]:
    """The Job name of each part, in order."""
    from .postprocess_job import _short_job_name  # noqa: PLC0415
    return [_short_job_name("robovast-postproc-", campaign_id, s.name) for s in parts]


class _MapPhase:
    """One campaign's map: its parts' Jobs, and what they are doing.

    Split from :func:`run_map_phase` so each step of it -- build, submit, wait, verdict --
    is its own answer, and the wait reads as the loop it is.
    """

    def __init__(self, campaign_id: str, campaign_root: str, namespace: str, parts,
                 core, batch, admission=None):
        from . import postprocess_job as pj  # noqa: PLC0415
        from .cluster_execution import _label_safe_campaign  # noqa: PLC0415

        self.campaign_id = campaign_id
        self.campaign_root = campaign_root
        self.namespace = namespace
        self.parts = parts
        self.names = part_job_names(campaign_id, parts)
        self.core, self.batch, self.admission = core, batch, admission
        self.owner = pj.postprocess_owner(campaign_id)
        self.label = (f"jobgroup={pj.POSTPROCESS_JOBGROUP},"
                      f"campaign-id={_label_safe_campaign(campaign_id)}")
        #: ``{job name: "succeeded" | "failed"}`` as the listing reports each Job's end.
        self.outcome: Dict[str, str] = {}
        #: Every part Job that has existed, so one the queue never created is noticed.
        self.ever_created: set = set()
        self.tracker = AdmittedJobs(
            admission=admission, owner=self.owner, batch_api=batch, core_api=core,
            namespace=namespace, label_selector=self.label,
            list_remaining=self._still_running)

    def _still_running(self, wanted):
        def record(name, status):
            if status.failed:
                self.outcome[name] = "failed"
            elif status.completion_time is not None:
                self.outcome[name] = "succeeded"

        return running_jobs(self.batch, self.namespace, self.label, wanted, on_status=record)

    def plan(self, image, image_cmds, host_cmds, *, force, tolerate_under, pull_secret,
             convert_resources):
        """Record each part's membership and render its Job. ``(items, error)``.

        *items* are what the queue creates, one per part this attempt is creating; a part
        Job an earlier attempt left running is adopted instead -- its scripts are mounted
        and running, and writing them again would swap the script out from under the
        interpreter (see ``postprocess_job.submit_postprocess_job``).
        """
        from robovast.common.errors import ClusterUnreachableError  # noqa: PLC0415
        from robovast.execution.campaign_archive import write_part  # noqa: PLC0415

        from . import postprocess_job as pj  # noqa: PLC0415

        items = []
        for part, name in zip(self.parts, self.names):
            write_part(self.campaign_root, part.name, part.runs, part.jobs)
            try:
                steps = pj.image_steps_for(self.campaign_id, self.campaign_root, image_cmds,
                                           force=force, tolerate_under=tolerate_under,
                                           part=part.name)
            except (KeyError, ValueError, ImportError, FileNotFoundError,
                    AttributeError) as e:
                return [], f"postprocessing cannot run its execution-image steps: {e}"
            manifest = pj.build_manifest(
                self.campaign_id, image, steps, self.namespace,
                role=pj.JobRole.for_part(part.name, host_cmds), force=force,
                pull_secret_name=pull_secret, convert_resources=convert_resources,
                stage_bytes=pj.stage_bytes(self.campaign_root, skip_bags=not steps,
                                           batch_jobs="", part=part.name))
            try:
                with api_transport_errors("submitting the postprocessing jobs"):
                    live = pj.live_job(self.batch, self.core, self.namespace, name)
            except ClusterUnreachableError as e:
                return [], f"postprocessing cannot be scheduled: {e}"
            if live:
                self.tracker.adopt([name])
                self.ever_created.add(name)
                continue
            items.append((name, pj.pod_sizing(manifest),
                          partial(_create_part_job, self.core, self.batch, self.namespace,
                                  self.campaign_id, name, manifest, steps, part.name)))
        return items, ""

    def submit(self, items) -> str:
        """Queue the parts. The refusal to report, or ``""``."""
        from . import postprocess_job as pj  # noqa: PLC0415
        from .node_admission import (AdmissionRefused,  # noqa: PLC0415
                                     campaign_start_key)

        if not items:
            return ""
        if self.admission is not None:
            try:
                # One part's request stands for all of them: they are sized alike.
                self.admission.preflight(items[0][1])
            except AdmissionRefused as exc:
                return (f"a postprocessing part needs more than any node in this cluster "
                        f"has. Lower results_processing.resources. ({exc})")
        self.tracker.submit(items, started_at=campaign_start_key(self.campaign_id),
                            priority=pj.POSTPROCESS_PRIORITY, campaign=self.campaign_id)
        return ""

    def wait(self, timeout: float, should_stop=None) -> tuple:
        """Poll until every part is done. ``(ok, message)``; ``ok`` None means unknown."""
        from robovast.common.errors import ClusterUnreachableError  # noqa: PLC0415

        from . import postprocess_job as pj  # noqa: PLC0415

        deadline = time.monotonic() + timeout
        last_log = 0.0
        settled = False
        try:
            while True:
                if should_stop is not None and should_stop():
                    self.cancel()
                    return False, "postprocessing cancelled: the campaign was stopped"
                try:
                    with api_transport_errors("waiting for the postprocessing jobs"):
                        rnd = self.tracker.poll()
                except ClusterUnreachableError as e:
                    return None, f"lost sight of the postprocessing jobs: {e}"
                self.ever_created.update(rnd.created)
                if time.monotonic() - last_log >= pj.LIVE_LOG_INTERVAL:
                    last_log = time.monotonic()
                    self.publish_log()
                if rnd.expired:
                    reasons = "; ".join(sorted({(rnd.blocked or {}).get(n, "")
                                                for n in rnd.expired}))
                    self.cancel()
                    return False, (f"{len(rnd.expired)} postprocessing part(s) could not "
                                   f"start: {reasons}")
                if rnd.over:
                    settled = True
                    return self.verdict()
                if time.monotonic() > deadline:
                    return None, (f"the postprocessing parts were still running after "
                                  f"{timeout:g}s; they continue in the cluster")
                time.sleep(pj.POLL_SECONDS)
        finally:
            if not settled and self.admission is not None:
                # Whatever ended the wait early, a part still queued must not be created
                # later with nobody waiting for it.
                self.admission.cancel(self.owner)
            self.publish_log()

    def verdict(self) -> tuple:
        """What the finished parts amount to: ``(ok, message)``."""
        from . import postprocess_job as pj  # noqa: PLC0415

        never = [n for n in self.names if n not in self.ever_created]
        if never:
            reason = self.admission.refusal(self.owner) if self.admission is not None else ""
            return False, (f"{len(never)} postprocessing part(s) were never created"
                           + (f": {reason}" if reason else ""))
        failed = [n for n in self.names if self.outcome.get(n) == "failed"]
        if failed:
            why = "; ".join(f"{n}: {pj.pod_failure_reason(self.core, self.namespace, n)}"
                            for n in failed)
            return False, (f"{len(failed)} of {len(self.names)} postprocessing part(s) "
                           f"failed -- {why}")
        return True, f"{len(self.names)} postprocessing part(s) complete"

    def cancel(self) -> None:
        """Delete every part Job of this campaign."""
        from . import postprocess_job as pj  # noqa: PLC0415

        for name in self.names:
            pj.cancel_job(self.batch, self.namespace, name)

    def publish_log(self) -> None:
        """The parts' output so far, as the campaign's POSTPROCESSING section."""
        from . import postprocess_job as pj  # noqa: PLC0415

        pj.write_phase_log(self.campaign_root,
                           live_map_log(self.core, self.namespace, self.parts, self.names))


def run_map_phase(cluster_config, campaign_id: str, campaign_root: str, namespace: str,
                  image, map_cmds: list, parts: List[Part], *, token: str,
                  force: bool = False, kube_context=None, tolerate_under=(),
                  convert_resources=None, admission=None, should_stop=None,
                  timeout: float = None) -> tuple:
    """Run *map_cmds* in one Job per part and wait for all of them. ``(ok, message)``.

    ``ok`` is three-valued like :func:`~.postprocess_job.run_conversion_job`'s: ``None``
    when this process lost sight of the Jobs (the wait ran out while they were active),
    which says nothing about whether they will succeed.

    Each part's Job stages its own runs, runs the map's image steps in its image container
    and its host steps in its host container, and delivers what it derived -- including its
    own log, provenance and usage record, named for the part.
    """
    from kubernetes import client  # noqa: PLC0415
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    from robovast.common.errors import ClusterUnreachableError  # noqa: PLC0415
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        needs_execution_image)

    from . import postprocess_job as pj  # noqa: PLC0415
    from .cluster_execution import resolve_pull_secret  # noqa: PLC0415
    from .kube_client import load_kube_config  # noqa: PLC0415

    config_dir = os.path.dirname(pj.campaign_vast(campaign_root))
    image_cmds = [c for c in map_cmds if needs_execution_image(c, config_dir)]
    host_cmds = [c for c in map_cmds if not needs_execution_image(c, config_dir)]
    if image_cmds and not image:
        return False, ("no execution image for the campaign's image steps; its custom ROS2 "
                       "types deserialize in no other image")

    load_kube_config(kube_context)
    core, batch = client.CoreV1Api(), client.BatchV1Api()
    try:
        with api_transport_errors("submitting the postprocessing jobs"):
            pull_secret = resolve_pull_secret(cluster_config, core, namespace)
            # Once for every part: they share the campaign's token, and its Secret must
            # exist before the first Job that names it.
            pod_access.ensure_campaign_secret(core, namespace, campaign_id, token)
    except ClusterUnreachableError as e:
        return False, f"postprocessing cannot be scheduled: {e}"
    except ApiException as e:
        return False, f"could not write the campaign's data-plane token Secret: {e}"

    phase = _MapPhase(campaign_id, campaign_root, namespace, parts, core, batch, admission)
    items, error = phase.plan(image, image_cmds, host_cmds, force=force,
                              tolerate_under=tolerate_under, pull_secret=pull_secret,
                              convert_resources=convert_resources)
    if error:
        return False, error
    error = phase.submit(items)
    if error:
        return False, error
    logger.info("Postprocessing %s in %d parallel part(s)%s", campaign_id, len(parts),
                f", {len(phase.ever_created)} of them already running"
                if phase.ever_created else "")
    return phase.wait(pj.DEFAULT_TIMEOUT if timeout is None else timeout,
                      should_stop=should_stop)


def _create_part_job(core, batch, namespace: str, campaign_id: str, name: str,
                      manifest: dict, steps: list, part: str, node_id=None) -> None:
    """Create one part's Job where admission made room, with the scripts it mounts.

    The same submission the campaign-level Job goes through
    (:func:`~.postprocess_job.submit_postprocess_job`); the Secret is already written for
    every part of this campaign, so only the Job and its scripts are this part's. It
    raises on failure, which is what the queue's create callback wants: the queue retries,
    and records the reason as the owner's refusal once it gives up.
    """
    from . import postprocess_job as pj  # noqa: PLC0415

    pin_campaign_job(manifest, node_id)
    pj.submit_postprocess_job(core, batch, namespace, campaign_id, name, manifest, steps,
                              discriminator=part)


def live_map_log(core, namespace: str, parts, names) -> str:
    """The parts' output so far, one headed block per part, for the POSTPROCESSING section."""
    from .postprocess_job import read_job_log  # noqa: PLC0415

    blocks = []
    for index, (part, name) in enumerate(zip(parts, names), start=1):
        text = read_job_log(core, namespace, name)
        if text:
            blocks.append(_block(part.name, index, len(parts), len(part.runs), text))
    return "".join(blocks)


def delivered_map_log(campaign_root: str, parts) -> str:
    """The parts' logs as they delivered them, in the same blocks as :func:`live_map_log`.

    Read from the campaign once the parts are done: a finished pod's log lasts only as long
    as its Job, while the file a part delivered stays with the campaign.
    """
    from robovast.execution.campaign_archive import part_file  # noqa: PLC0415

    blocks = []
    for index, part in enumerate(parts, start=1):
        path = os.path.join(campaign_root, *part_file(part.name, "postprocessing.log").split("/"))
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        blocks.append(_block(part.name, index, len(parts), len(part.runs), text))
    return "".join(blocks)


def _block(part: str, index: int, total: int, runs: int, text: str) -> str:
    """One part's output under a heading a reader can place: which part, and of how many."""
    return (f"===== postprocessing {part} ({index} of {total}, {runs} run(s)) =====\n"
            + (text if text.endswith("\n") else text + "\n"))
