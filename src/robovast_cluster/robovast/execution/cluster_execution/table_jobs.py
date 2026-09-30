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

"""A campaign's tables built by Jobs on the cluster, a part of its runs each, then merged.

The decoding a table build is made of is per run, so the runs are split into parts
(:mod:`robovast.results_processing.table_parts`) and each part is built by a Job of its own:

* ``stage`` (init container, the sidecar image) fetches the part's records from the data plane
  -- the archive route narrowed to the part, the records alone, a plain tar -- into an
  ``emptyDir``;
* ``build`` (the controller image, the one that carries the decoder) builds and compacts the
  part's tables there and delivers them into the campaign's cache over the outputs route.

**A part is sized like the system under test on its node**: what the campaign recorded the SUT
was given there (``campaign.db``, ``node.calibration_json``) where the campaign calibrated, its
declaration where it did not, request equal to limit (:func:`recorded_sizing`). Read from the
record rather than from a running campaign, so postprocessing started again later sizes its
parts as the campaign-end pass would have. A
node sized for one run of the system under test is sized for a part of its runs' decoding,
and a pod the size of a pod the node already ran is one the queue can place. Its workers are
its CPU rounded up, and its disk request what it stages with room to build. A smaller level's
fewer workers ask for no more CPU than they can use, so more of them fit at once.

**Out of memory, a level down** (:mod:`.shrinking_jobs`): parts start at :data:`RUNS_PER_PART`
runs; a part that is OOM-killed takes its level back, and what was not delivered is built again
in parts of half the runs with half the workers. Memory stays the SUT's. A part of one job's
runs with one worker that is still killed does not fit, and the build fails naming it.

The service merges the delivered parts into one compacted file per table and the parts are
removed (:func:`~robovast.results_processing.table_parts.merge_parts`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shlex
import uuid
from typing import Callable, Iterable, List, Optional

from robovast.results_processing.table_parts import (Part, clear_parts, merge_parts, plan_units,
                                                     write_part)

from .campaign_job import campaign_job_manifest, pin_campaign_job
from .cluster_execution import _label_safe_campaign
from .node_admission import JobSizing
from .pod_access import campaign_pod_env, deliver_command, fetch_command
from .shrinking_jobs import Item, ShrinkingJobs

logger = logging.getLogger(__name__)

#: The jobgroup label of a table-building Job.
TABLES_JOBGROUP = "table-build"

#: Runs a part starts with. Each level down halves it.
RUNS_PER_PART = 100

#: Set in the service's environment to start parts at another size.
RUNS_PER_PART_ENV = "ROBOVAST_TABLE_PART_RUNS"

#: Where the part is staged and built in the pod.
WORK_DIR = "/work"

#: Disk a part asks for, per byte it stages: the records, and the tables built beside them.
EPHEMERAL_PER_STAGED_BYTE = 1.5

#: A finished Job is kept this long, so its log can be read.
TABLES_JOB_TTL_SECONDS = 600

#: Queue priority: a campaign's own tables, after its runs, like its other end-of-campaign work.
TABLES_PRIORITY = 2

#: Tables the service merges at once: the same bound its own compacting has.
MERGE_WORKERS = 8


def runs_per_part() -> int:
    raw = (os.environ.get(RUNS_PER_PART_ENV) or "").strip()
    if not raw:
        return RUNS_PER_PART
    value = int(raw)
    if value < 1:
        raise ValueError(f"{RUNS_PER_PART_ENV}={raw!r}: a part holds at least one run")
    return value


def job_name(campaign_id: str, build: str, level: int, index: int) -> str:
    """A DNS-safe Job name, unique per campaign, build, level and part.

    *build* names one build: a campaign's tables are built again by a later postprocessing
    while the Jobs of the last build are still kept for their logs.
    """
    digest = hashlib.sha1(campaign_id.encode()).hexdigest()[:10]
    return f"rv-tables-{digest}-{build}-g{level}-p{index}"


def _staged_bytes(campaign_dir: str, part: Part) -> int:
    total = 0
    for rel in part.runs + part.jobs:
        for root, _dirs, names in os.walk(os.path.join(campaign_dir, rel)):
            for name in names:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    return total


def _pack(units: List[Part], budget: int) -> List[tuple]:
    """*units* in parts of at most *budget* runs, in order; ``[(part, its units)]``. A unit
    larger than the budget is a part alone."""
    parts, current, members = [], Part(name=""), []
    for unit in units:
        if current.runs and len(current.runs) + len(unit.runs) > budget:
            parts.append((current, members))
            current, members = Part(name=""), []
        current.runs += unit.runs
        current.jobs += unit.jobs
        members.append(unit)
    if current.runs:
        parts.append((current, members))
    return parts


class TablePods:
    """Build *campaign_dir*'s tables in Jobs; what a backend's ``build_tables`` does.

    *sizing_on* ``(node_id) -> (cpu, memory_bytes)`` is the system under test's size on a node
    (``None`` for a node the queue has not chosen); *max_cpu* the largest of those, which
    sets the starting workers. *images* is ``(sidecar, controller)``.
    """

    def __init__(self, *, campaign_id: str, campaign_dir: str, namespace: str, batch_api,
                 core_api, admission, images, pull_secret: str, started_at: float,
                 sizing_on: Callable, max_cpu: float, create_job: Callable,
                 poll_seconds: float = 5.0):
        self.campaign_id = campaign_id
        self.campaign_dir = campaign_dir
        self.namespace = namespace
        self.batch_api = batch_api
        self.core_api = core_api
        self.admission = admission
        self.sidecar_image, self.controller_image = images
        self.pull_secret = pull_secret
        self.started_at = started_at
        self.sizing_on = sizing_on
        self.max_workers = max(1, math.ceil(max_cpu))
        self.create_job = create_job
        self.poll_seconds = poll_seconds
        self.budget = runs_per_part()
        self.label_selector = (f"jobgroup={TABLES_JOBGROUP},"
                               f"campaign-id={_label_safe_campaign(campaign_id)}")
        #: ``{job name: (level, part name)}`` of every Job submitted.
        self.parts = {}
        #: This build's name among the campaign's builds (:func:`job_name`).
        self.build = uuid.uuid4().hex[:6]

    def workers(self, cpu: float, level: int) -> int:
        return max(1, math.ceil(cpu) >> level)

    def size(self, node_id, level: int):
        """``(cpu, memory, workers)`` of a part of *level* on *node_id*: the SUT's memory, and
        no more CPU than the level's workers use."""
        cpu, memory = self.sizing_on(node_id)
        workers = self.workers(cpu, level)
        return (cpu if level == 0 else min(cpu, float(workers))), memory, workers

    def _manifest(self, name: str, part: Part, level: int, tables, node_id) -> dict:
        cpu, memory, workers = self.size(node_id, level)
        ephemeral = int(_staged_bytes(self.campaign_dir, part) * EPHEMERAL_PER_STAGED_BYTE)
        env = campaign_pod_env(self.namespace, self.campaign_id)
        stage = fetch_command(f"/campaigns/{self.campaign_id}/archive", WORK_DIR,
                              f"raw=true&part={part.name}&uncompressed=true")
        build = " ".join(shlex.quote(a) for a in (
            ["python3", "-m", "robovast.results_processing.table_parts",
             f"{WORK_DIR}/{self.campaign_id}", "--part", part.name, "--generation", str(level),
             "--workers", str(workers), "--out", f"{WORK_DIR}/out"]
            + [a for t in (tables or []) for a in ("--table", t)]))
        deliver = deliver_command(f"{WORK_DIR}/out", f"/campaigns/{self.campaign_id}/outputs")
        resources = {"requests": {"cpu": str(cpu), "memory": str(memory),
                                  "ephemeral-storage": str(ephemeral)},
                     "limits": {"cpu": str(cpu), "memory": str(memory)}}
        mount = [{"name": "work", "mountPath": WORK_DIR}]
        manifest = campaign_job_manifest(
            name=name, namespace=self.namespace, jobgroup=TABLES_JOBGROUP,
            campaign_id=self.campaign_id, ttl_seconds=TABLES_JOB_TTL_SECONDS,
            pull_secret=self.pull_secret, pod_name="table-build",
            pod_spec={
                "initContainers": [{"name": "stage", "image": self.sidecar_image,
                                    "command": ["sh", "-c", stage], "env": env,
                                    "volumeMounts": mount}],
                "containers": [{"name": "build", "image": self.controller_image,
                                "command": ["sh", "-c", f"{build} && {deliver}"],
                                "env": env, "resources": resources, "volumeMounts": mount}],
                "volumes": [{"name": "work", "emptyDir": {}}],
            })
        return pin_campaign_job(manifest, node_id)

    def _sizing(self, level: int):
        """``(node_id) -> JobSizing``: what the queue counts a part of *level* as on a node;
        the manifest asks for the same."""
        def sizing(node_id) -> JobSizing:
            cpu, memory, _workers = self.size(node_id, level)
            return JobSizing(cpu, memory)
        return sizing

    def _items(self, tables):
        def make_items(units: List[Part], level: int) -> List[Item]:
            items = []
            for index, (part, members) in enumerate(_pack(list(units),
                                                          max(1, self.budget >> level))):
                part.name = f"g{level}-p{index}"
                write_part(self.campaign_dir, part)
                name = job_name(self.campaign_id, self.build, level, index)
                self.parts[name] = (level, part.name)

                def create(node_id=None, name=name, part=part, level=level):
                    self.create_job(self._manifest(name, part, level, tables, node_id))

                items.append(Item(name, self._sizing(level)(None), create, members))
            return items
        return make_items

    def run(self, tables: Optional[Iterable[str]],
            should_stop: Optional[Callable[[], bool]] = None):
        """Build the tables in parts, merge them, and clear the parts; what the merge compacted."""
        tables = sorted(tables) if tables is not None else None
        clear_parts(self.campaign_dir)
        units = plan_units(self.campaign_dir)
        work = ShrinkingJobs(
            admission=self.admission, owner_prefix=f"{self.campaign_id}#tables",
            batch_api=self.batch_api, core_api=self.core_api, namespace=self.namespace,
            label_selector=self.label_selector, make_items=self._items(tables),
            at_floor=lambda item, level: (len(item.units) == 1
                                          and self.workers(self.max_workers, level) == 1),
            submit_kwargs=lambda level: {
                "started_at": self.started_at, "priority": TABLES_PRIORITY,
                "campaign": self.campaign_id, "sizing_for_node": self._sizing(level)},
            poll_seconds=self.poll_seconds)
        try:
            delivered = work.run(units, should_stop=should_stop)
            for line in work.shrinks:
                logger.warning("tables of %s: %s", self.campaign_id, line)
            return merge_parts(self.campaign_dir, sorted(self.parts[n] for n in delivered),
                               workers=min(MERGE_WORKERS, os.cpu_count() or 1))
        finally:
            clear_parts(self.campaign_dir)


def _cpu_memory(resources) -> Optional[tuple]:
    """``(cpu, memory bytes)`` a container's resources request, or ``None`` without both."""
    from .kube_client import parse_resource  # noqa: PLC0415
    requests = (resources or {}).get("requests") or resources or {}
    cpu, memory = requests.get("cpu"), requests.get("memory")
    if not cpu or not memory:
        return None
    return parse_resource(cpu), int(parse_resource(memory))


def recorded_sizing(campaign_dir: str):
    """``(sizing_on, max_cpu)`` for a campaign's table parts, from its ``campaign.db``; ``None``
    when nothing in the record says how large a part may be.

    On a node the campaign calibrated, the resources its system under test was given there
    (``node.calibration_json``); a campaign without a system under test is sized like its
    main container. Elsewhere, the system under test's declared resources, or where the
    ``.vast`` declared none (calibrated sizing declares nothing), the largest allocation the
    record holds: a part the size of what some node gave a run is one the queue can place.
    """
    import sqlite3  # noqa: PLC0415

    from robovast.common.config import SUT_CONTAINER  # noqa: PLC0415

    from .manifests import MAIN_CONTAINER_NAME  # noqa: PLC0415

    path = os.path.join(campaign_dir, "campaign.db")
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        nodes = conn.execute("SELECT node_label, calibration_json FROM node").fetchall()
        row = conn.execute("SELECT config_json FROM campaign LIMIT 1").fetchone()
    finally:
        conn.close()
    by_node = {}
    for label, calibration in nodes:
        allocated = (json.loads(calibration) if calibration else {}).get("allocated") or {}
        size = _cpu_memory(allocated.get(SUT_CONTAINER)) or _cpu_memory(
            allocated.get(MAIN_CONTAINER_NAME))
        if size:
            by_node[label] = size
    containers = ((json.loads(row[0]) if row and row[0] else {}).get("execution") or {}).get(
        "containers") or {}
    declared = _cpu_memory((containers.get(SUT_CONTAINER) or {}).get("resources"))
    default = declared or (max(by_node.values()) if by_node else None)
    if default is None:
        return None

    def sizing_on(node_id):
        return by_node.get(node_id, default)

    return sizing_on, max(size[0] for size in [default, *by_node.values()])


def pod_builder(campaign_id: str, campaign_dir: str, *, namespace: str, batch_api, core_api,
                admission, images, pull_secret: str, should_stop=None):
    """What builds *campaign_dir*'s tables in Jobs, as ``(tables) -> merge report``; ``None``
    when the record says nothing about how large a part may be (:func:`recorded_sizing`)."""
    from .node_admission import campaign_start_key  # noqa: PLC0415
    sizing = recorded_sizing(campaign_dir)
    if sizing is None:
        return None
    sizing_on, max_cpu = sizing

    def build(tables):
        return TablePods(
            campaign_id=campaign_id, campaign_dir=campaign_dir, namespace=namespace,
            batch_api=batch_api, core_api=core_api, admission=admission, images=images,
            pull_secret=pull_secret, started_at=campaign_start_key(campaign_id),
            sizing_on=sizing_on, max_cpu=max_cpu,
            create_job=lambda manifest: batch_api.create_namespaced_job(namespace, manifest),
        ).run(tables, should_stop)

    return build


def cluster_pod_builder(campaign_dir: str, *, namespace: str, kube_context, cluster_config,
                        admission, sidecar_image: Optional[str] = None, should_stop=None):
    """:func:`pod_builder` on the cluster *kube_context* reaches: its clients, the campaign's
    sidecar (*sidecar_image*, or the deployment's own), the controller image and the pull
    Secret. What the campaign-end pass and a postprocessing started later both use.

    ``None`` when the record says nothing about how large a part may be. Nothing that reaches
    the cluster happens before the tables are asked for: *cluster_config* and *admission* are
    callables, and a cluster that cannot be reached then fails the build, which the pass
    reports and builds in its own process instead.
    """
    if recorded_sizing(campaign_dir) is None:
        return None

    def build(tables):
        from kubernetes import client  # noqa: PLC0415

        from robovast.common.execution import resolve_controller_image  # noqa: PLC0415
        from robovast.common.execution import resolve_sidecar_image

        from .cluster_execution import resolve_pull_secret  # noqa: PLC0415
        from .kube_client import core_v1_client  # noqa: PLC0415

        core = core_v1_client(context=kube_context)
        builder = pod_builder(
            os.path.basename(os.path.normpath(campaign_dir)), campaign_dir,
            namespace=namespace, batch_api=client.BatchV1Api(), core_api=core,
            admission=admission(),
            images=(sidecar_image or resolve_sidecar_image(), resolve_controller_image()),
            pull_secret=resolve_pull_secret(cluster_config(), core, namespace),
            should_stop=should_stop)
        return builder(tables)

    return build


__all__ = ["RUNS_PER_PART", "RUNS_PER_PART_ENV", "TABLES_JOBGROUP", "TablePods",
           "cluster_pod_builder", "job_name", "pod_builder", "recorded_sizing"]
