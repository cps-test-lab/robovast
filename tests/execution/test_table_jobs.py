# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign's tables built by Jobs, one part each, answer as the campaign built whole.

The cluster is a fake whose "pods" really run: a Job created stages its part through the
archive generator, runs the pod's own command line, and delivers what it built into the
campaign as the outputs route does.
"""

import io
import os
import re
import shlex
import shutil
import tarfile
import types

import pytest

from robovast.execution import campaign_archive
from robovast.execution.cluster_execution.table_jobs import (TablePods, job_name,
                                                              recorded_sizing)
from robovast.results_processing import table_parts
from robovast_data import Campaign
from robovast_decode.tables import read_manifest, compacted_runs
from tests.robovast_data.conftest import nav_campaign

RUNS = tuple((c, r) for c in ("cfg-a", "cfg-b") for r in range(4))
QUERY = ("SELECT config_name, run_id, count(*) n, sum(\"position.x\") x FROM poses "
         "GROUP BY 1, 2 ORDER BY 1, 2")
MIB = 1024 * 1024


class _Cluster:
    """Jobs that run their pod in this process when created, and end on the next listing."""

    def __init__(self, campaign, tmp_path, oom=lambda part, level: False):
        self.campaign = campaign
        self.tmp = tmp_path
        self.oom = oom
        self.jobs = {}
        self.manifests = []
        self.killed = set()
        self.deleted = []

    def create(self, manifest):
        self.manifests.append(manifest)
        name = manifest["metadata"]["name"]
        build = manifest["spec"]["template"]["spec"]["containers"][0]["command"][2]
        args = shlex.split(build.split(" && ")[0])
        part = args[args.index("--part") + 1]
        level = int(args[args.index("--generation") + 1])
        if self.oom(part, level):
            self.killed.add(name)
            self.jobs[name] = types.SimpleNamespace(active=None, succeeded=None, failed=1,
                                                    completion_time=None)
            return
        work = self.tmp / "pods" / name
        payload = b"".join(campaign_archive.iter_campaign_tar(
            str(self.campaign), campaign_archive.RAW, part=part, compress=False))
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r|") as tar:
            tar.extractall(work, filter="data")
        out = work / "out"
        args = [a.replace("/work", str(work)) for a in args]
        assert table_parts.main(args[3:]) == 0
        for dirpath, _dirs, names in os.walk(out):
            for n in names:
                src = os.path.join(dirpath, n)
                dst = self.campaign / os.path.relpath(src, out)
                os.makedirs(dst.parent, exist_ok=True)
                shutil.copy2(src, dst)
        self.jobs[name] = types.SimpleNamespace(active=None, succeeded=1, failed=None,
                                                completion_time="t")

    def list_namespaced_job(self, namespace, label_selector):
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(metadata=types.SimpleNamespace(name=n), status=s)
            for n, s in self.jobs.items()])

    def delete_namespaced_job(self, name, namespace, propagation_policy):
        self.deleted.append(name)
        self.jobs.pop(name, None)

    def list_namespaced_pod(self, namespace, label_selector=None, **_kw):
        return types.SimpleNamespace(items=[])


@pytest.fixture
def campaign(tmp_path):
    return nav_campaign(tmp_path / "service" / "nav-2026-01-01-00000000", runs=RUNS)


def _pods(campaign, cluster, monkeypatch, runs_per_part, cpu=3.0):
    monkeypatch.setenv("ROBOVAST_TABLE_PART_RUNS", str(runs_per_part))
    pods = TablePods(campaign_id=campaign.name, campaign_dir=str(campaign), namespace="ns",
                     batch_api=cluster, core_api=cluster, admission=None,
                     images=("sidecar:1", "controller:1"), pull_secret="", started_at=0.0,
                     sizing_on=lambda node_id: (cpu, 512 * MIB), max_cpu=cpu,
                     create_job=cluster.create, poll_seconds=0)
    import robovast.execution.cluster_execution.shrinking_jobs as sj
    monkeypatch.setattr(sj, "oom_killed_job_forensics",
                        lambda core, ns, sel, job_names=None:
                        {n: {} for n in job_names if n in cluster.killed})
    return pods


def _reference(campaign, tmp_path):
    whole = tmp_path / "reference" / campaign.name
    shutil.copytree(campaign, whole, symlinks=True)
    Campaign(str(whole)).build(workers=1, progress=False)
    return Campaign(str(whole), workers=1).sql(QUERY).to_dict("records")


def test_tables_built_in_parts_by_jobs_answer_as_the_campaign_built_whole(
        campaign, tmp_path, monkeypatch):
    want = _reference(campaign, tmp_path)
    cluster = _Cluster(campaign, tmp_path)
    report = _pods(campaign, cluster, monkeypatch, runs_per_part=3).run(None)
    assert report.compacted["poses"] == len(RUNS)
    assert len(cluster.manifests) == 3
    assert compacted_runs(read_manifest(str(campaign)), "poses") == {f"{c}/{r}" for c, r in RUNS}
    assert not (campaign / ".cache" / "parts").exists()
    assert not (campaign / "_execution" / "table_parts").exists()
    assert Campaign(str(campaign), workers=1).sql(QUERY).to_dict("records") == want


def test_a_part_that_runs_out_of_memory_is_built_again_smaller(campaign, tmp_path,
                                                               monkeypatch):
    want = _reference(campaign, tmp_path)
    cluster = _Cluster(campaign, tmp_path, oom=lambda part, level: level == 0 and part == "g0-p1")
    pods = _pods(campaign, cluster, monkeypatch, runs_per_part=4)
    pods.run(None)
    levels = [int(re.search(r"-g(\d+)-", m["metadata"]["name"]).group(1))
              for m in cluster.manifests]
    assert levels.count(0) == 2 and 1 in levels
    build = [m["spec"]["template"]["spec"]["containers"][0]["command"][2]
             for m in cluster.manifests]
    assert all("--workers 3" in b for b in build[:2])
    assert all("--workers 1" in b for b in build[2:])
    cpus = [m["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"]
            for m in cluster.manifests]
    assert cpus[:2] == ["3.0", "3.0"] and set(cpus[2:]) == {"1.0"}  # CPU follows the workers
    assert Campaign(str(campaign), workers=1).sql(QUERY).to_dict("records") == want


def test_a_job_pod_is_sized_like_the_system_under_test_and_stages_its_part(
        campaign, tmp_path, monkeypatch):
    cluster = _Cluster(campaign, tmp_path)
    pods = _pods(campaign, cluster, monkeypatch, runs_per_part=8, cpu=2.5)
    pods.run(["poses"])
    (manifest,) = cluster.manifests
    pod = manifest["spec"]["template"]["spec"]
    assert manifest["metadata"]["name"] == job_name(campaign.name, pods.build, 0, 0)
    build = pod["containers"][0]
    assert build["resources"]["requests"]["cpu"] == build["resources"]["limits"]["cpu"] == "2.5"
    assert build["resources"]["limits"]["memory"] == str(512 * MIB)
    assert int(build["resources"]["requests"]["ephemeral-storage"]) > 0
    assert "--workers 3" in build["command"][2] and "--table poses" in build["command"][2]
    stage = pod["initContainers"][0]["command"][2]
    assert "raw=true&part=g0-p0&uncompressed=true" in stage
    assert manifest["metadata"]["labels"]["jobgroup"] == "table-build"


def _record(root, nodes, sut=None):
    """A ``campaign.db`` holding *nodes* (``{label: calibration}``) and a declared SUT."""
    from robovast.common.store import CampaignStore
    config = {"execution": {"containers": {"sut": {"resources": sut}}}} if sut else {}
    with CampaignStore(root / "campaign.db") as store:
        cid = store.create_campaign(root.name, config, mode="batch")
        store.set_node_facts_resolver(lambda label: {"calibration": nodes[label]}
                                      if nodes.get(label) else None)
        for i, label in enumerate(nodes):
            store.upsert_job(cid, f"_jobs/job-{i}", {"node_label": label}, store._node_facts)


def _allocated(**containers):
    return {"allocated": {name: {"requests": {"cpu": cpu, "memory": memory}}
                          for name, (cpu, memory) in containers.items()}}


def test_a_part_is_sized_from_what_the_record_says_the_sut_had_on_its_node(tmp_path):
    root = tmp_path / "camp-2026-01-01-00000000"
    root.mkdir()
    _record(root, {"node-a": _allocated(sut=("2.42", "896Mi"), robovast=("1", "512Mi")),
                   "node-b": _allocated(sut=("3.95", "1Gi")),
                   "node-c": None})
    sizing_on, max_cpu = recorded_sizing(str(root))
    assert sizing_on("node-a") == (2.42, 896 * MIB)
    assert sizing_on("node-b") == (3.95, 1024 * MIB)
    # A node with no record, or none chosen yet: the largest the record holds.
    assert sizing_on("node-c") == sizing_on(None) == (3.95, 1024 * MIB)
    assert max_cpu == 3.95


def test_without_a_calibration_a_part_is_sized_from_the_declared_sut(tmp_path):
    root = tmp_path / "camp-2026-01-01-00000000"
    root.mkdir()
    _record(root, {"node-a": None}, sut={"cpu": 2, "memory": "2Gi"})
    sizing_on, _max = recorded_sizing(str(root))
    assert sizing_on("node-a") == (2.0, 2048 * MIB)


def test_a_campaign_without_a_sut_is_sized_like_its_main_container(tmp_path):
    root = tmp_path / "camp-2026-01-01-00000000"
    root.mkdir()
    _record(root, {"node-a": _allocated(robovast=("1.5", "768Mi"))})
    sizing_on, _max = recorded_sizing(str(root))
    assert sizing_on("node-a") == (1.5, 768 * MIB)


def test_a_record_that_says_nothing_about_size_builds_nothing_here(tmp_path):
    root = tmp_path / "camp-2026-01-01-00000000"
    root.mkdir()
    _record(root, {"node-a": None})
    assert recorded_sizing(str(root)) is None
    assert recorded_sizing(str(tmp_path / "missing")) is None


def test_a_second_build_of_a_campaign_does_not_collide_with_the_first(campaign, tmp_path,
                                                                     monkeypatch):
    """A later postprocessing builds again while the last build's Jobs are kept for their
    logs: the names must differ."""
    cluster = _Cluster(campaign, tmp_path)
    first = _pods(campaign, cluster, monkeypatch, runs_per_part=8)
    first.run(["poses"])
    second = _pods(campaign, cluster, monkeypatch, runs_per_part=8)
    second.run(["poses"])
    names = [m["metadata"]["name"] for m in cluster.manifests]
    assert len(names) == len(set(names)) == 2
