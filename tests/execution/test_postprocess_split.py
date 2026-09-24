# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign's postprocessing split across Jobs: which steps, which runs, which files.

The run-scoped prefix of the postprocessing (the *map*) runs in one Job per part of the
campaign's runs; the rest (the *reduce*) and the campaign's completion run in one Job after
them. A part is a set of whole scenario jobs with their runs, and every file each part
would write is named for it, so the parts' deliveries never touch one path.
"""

import json
import os

import pytest
import yaml

from robovast.execution.campaign_archive import (PARTS_DIR, part_file, part_include,
                                                 stage_include, write_part)
from robovast.execution.cluster_execution import postprocess_job as pj
from robovast.execution.cluster_execution import postprocess_parts as ps
from robovast.results_processing.postprocessing import (STAGED_PROVENANCE,
                                                        _staged_provenance_entries,
                                                        split_postprocessing)

from .image_steps_helper import steps

# -- which steps -------------------------------------------------------------


def test_the_map_is_the_leading_run_scoped_steps():
    commands = [{"rosbags_process": {"plugins": [{"type": "to_csv"}]}}, "run_log",
                "resource_usage", "compress", "run_log"]
    map_cmds, reduce_cmds = split_postprocessing(commands, "")
    assert map_cmds == commands[:3]
    assert reduce_cmds == ["compress", "run_log"], (
        "a run-scoped step after a campaign-scoped one may read what it wrote")


def test_the_rosbag_shorthand_is_run_scoped():
    map_cmds, _ = split_postprocessing([{"rosbags_to_csv": {"topics": ["/a"]}}], "")
    assert map_cmds


def test_a_step_that_says_nothing_is_never_split(tmp_path):
    (tmp_path / "plugin.py").write_text(
        "from robovast.results_processing.postprocessing_plugins import "
        "BasePostprocessingPlugin\n"
        "class Mine(BasePostprocessingPlugin):\n"
        "    def __call__(self, results_dir, config_dir, **kw):\n"
        "        return True, 'ok'\n")
    map_cmds, reduce_cmds = split_postprocessing(["./plugin.py:Mine", "run_log"],
                                                 str(tmp_path))
    assert map_cmds == [] and reduce_cmds == ["./plugin.py:Mine", "run_log"]


def test_an_unresolvable_step_is_campaign_scoped():
    assert split_postprocessing(["no_such_plugin"], "") == ([], ["no_such_plugin"])


# -- which runs --------------------------------------------------------------


def _campaign(root, jobs):
    """*jobs*: ``{job: {run: bag bytes}}``; each job also gets a rosout bag of 10 bytes."""
    links = {}
    for job, runs in jobs.items():
        bag = root / "_jobs" / "batch-0" / job / "logs" / "rosout_bag"
        bag.mkdir(parents=True)
        (bag / "b.mcap").write_bytes(b"x" * 10)
        for run, size in runs.items():
            run_dir = root / run
            (run_dir / "rosbag2").mkdir(parents=True)
            (run_dir / "rosbag2" / "b.mcap").write_bytes(b"x" * size)
            (run_dir / "test.xml").write_text("<t/>")
            links[f"{run}/job"] = f"../../_jobs/batch-0/{job}"
    (root / "_transient").mkdir(parents=True, exist_ok=True)
    (root / "_transient" / "job_links.yaml").write_text(yaml.safe_dump(links))
    return root


def test_a_cap_of_one_or_a_single_unit_does_not_split(tmp_path):
    root = _campaign(tmp_path, {"job-0": {"cfg/0": 5, "cfg/1": 5}})
    assert ps.plan_parts(str(root), 1) == []
    assert ps.plan_parts(str(root), 4) == [], "one job is one unit"


def test_a_job_and_its_runs_are_never_split_and_parts_are_balanced(tmp_path):
    root = _campaign(tmp_path, {
        "job-0": {"a/0": 100, "a/1": 100},
        "job-1": {"a/2": 150},
        "job-2": {"b/0": 60},
        "job-3": {"b/1": 30},
    })
    parts = ps.plan_parts(str(root), 2)
    assert [s.name for s in parts] == ["part-1", "part-2"]
    by_run = {run: s.name for s in parts for run in s.runs}
    assert by_run["a/0"] == by_run["a/1"], "the runs of one job stay together"
    assert sorted(j for s in parts for j in s.jobs) == [
        f"_jobs/batch-0/job-{i}" for i in range(4)]
    weights = sorted(s.bytes for s in parts)
    assert weights[1] - weights[0] <= 210, "within one unit of each other"


def test_the_plan_weighs_bags_so_a_parts_outputs_do_not_move_it(tmp_path):
    root = _campaign(tmp_path, {"job-0": {"a/0": 50}, "job-1": {"a/1": 40},
                                "job-2": {"a/2": 30}})
    before = [(s.name, s.runs) for s in ps.plan_parts(str(root), 2)]
    (root / "a" / "2" / "poses.csv").write_bytes(b"y" * 10_000)
    assert [(s.name, s.runs) for s in ps.plan_parts(str(root), 2)] == before


def test_the_cap_is_a_whole_number_or_refused():
    assert ps.max_parallel(" 8 ") == 8
    for bad in ("0", "-2", "many", "1.5"):
        with pytest.raises(ValueError, match=ps.MAX_PARALLEL_ENV):
            ps.max_parallel(bad)


def test_unset_takes_the_cluster_rather_than_switching_the_split_off(monkeypatch):
    """A deployment that sets nothing postprocesses with the cluster it has, as a
    campaign's runs do -- so the parts are the cluster's size over one conversion's."""
    from robovast.execution.cluster_execution.cluster_capacity import (MAX_CPU_ENV,
                                                                       MAX_MEMORY_ENV)
    monkeypatch.setenv(MAX_CPU_ENV, "256")
    monkeypatch.setenv(MAX_MEMORY_ENV, "1024Gi")
    assert ps.max_parallel("", convert_cpu=4) == 64
    assert ps.max_parallel("", convert_cpu="8000m") == 32

    monkeypatch.delenv(MAX_CPU_ENV)
    assert ps.max_parallel("", convert_cpu=4) == ps.PARTS_WITHOUT_A_KNOWN_CLUSTER


# -- what a part stages -----------------------------------------------------


def test_a_part_stages_its_runs_their_jobs_and_everything_else(tmp_path):
    write_part(str(tmp_path), "part-2", ["a/1"], ["_jobs/batch-0/job-1"])
    include = part_include(str(tmp_path), "part-2")
    assert include("a", True) and include("a/1", True) and include("a/1/rosbag2/b.mcap", False)
    assert not include("a/0", True), "another part's run"
    assert include("a/_config/campaign.vast", False), "a configuration's own files"
    assert include("_jobs", True) and include("_jobs/batch-0", True)
    assert include("_jobs/batch-0/job-1/logs/rosout_bag/b.mcap", False)
    assert not include("_jobs/batch-0/job-10", True), "a prefix is not a job"
    assert not include("_jobs/batch-0/job-0", True)
    for rel in ("_config/c.vast", "_execution/execution.yaml", "_transient/job_links.yaml",
                "campaign.db"):
        assert include(rel, False), rel


def test_a_part_nobody_planned_is_refused(tmp_path):
    with pytest.raises(KeyError):
        part_include(str(tmp_path), "part-7")
    with pytest.raises(ValueError):
        part_include(str(tmp_path), "../x")


def test_the_part_and_the_stage_narrow_together(tmp_path):
    write_part(str(tmp_path), "part-1", ["a/0"], [])
    staged, in_part = stage_include(skip_bags=True), part_include(str(tmp_path), "part-1")
    assert in_part("a/0/rosbag2/b.mcap", False) and not staged("a/0/rosbag2/b.mcap", False)


# -- what a part's Job does -------------------------------------------------


@pytest.fixture
def dsn(monkeypatch):
    from robovast.common.index_db import DSN_ENV
    monkeypatch.setenv(DSN_ENV, "host=index.example.com dbname=robovast")


def _containers(manifest):
    spec = manifest["spec"]["template"]["spec"]
    return {c["name"]: c for c in spec["initContainers"] + spec["containers"]}


def test_a_parts_job_stages_its_part_and_writes_its_own_files(dsn):
    manifest = pj.build_manifest("camp", "img", steps("camp"), "ns",
                                 role=pj.JobRole.for_part("part-2", ["run_log"]))
    containers = _containers(manifest)
    stage = containers[pj.STAGE_CONTAINER]["command"][-1]
    assert "part=part-2" in stage and "batch_jobs" not in stage
    env = {e["name"]: e.get("value") for e in containers[pj.HOST_CONTAINER]["env"]}
    from robovast.execution.cluster_execution.postprocess_host import (ENV_COMMANDS,
                                                                       ENV_PART)
    assert env[ENV_PART] == "part-2" and json.loads(env[ENV_COMMANDS]) == ["run_log"]
    convert = containers[pj.CONVERT_CONTAINER]["command"][-1]
    assert f"{pj.CAMPAIGN_MOUNT}/camp/{part_file("part-2", 'postprocessing.log')}" in convert
    assert part_file("part-2", "system_usage.csv") in convert
    assert manifest["metadata"]["name"] != pj.campaign_job_name("camp")


def test_a_parts_image_steps_record_provenance_under_its_name(tmp_path):
    (tmp_path / "_config").mkdir()
    (tmp_path / "_config" / "c.vast").write_text("version: 1\n")
    rendered = pj.image_steps_for("camp", str(tmp_path),
                                  [{"rosbags_process": {"plugins": [{"type": "to_csv"}]}}],
                                  part="part-1")
    argv = rendered[0].argv
    assert argv[argv.index("--provenance-file") + 1] == (
        f"{pj.CAMPAIGN_MOUNT}/camp/{part_file('part-1', 'image.provenance.json')}")


def test_the_job_that_completes_a_split_skips_the_map(dsn):
    from robovast.execution.cluster_execution.postprocess_host import ENV_SKIP_MAP
    manifest = pj.build_manifest("camp", "img", [], "ns",
                                 role=pj.JobRole.reduce(stage_bags=False))
    env = {e["name"]: e.get("value") for e in _containers(manifest)[pj.HOST_CONTAINER]["env"]}
    assert env[ENV_SKIP_MAP] == "1"
    assert "skip_bags=true" in _containers(manifest)[pj.STAGE_CONTAINER]["command"][-1]


def test_every_parts_provenance_reaches_the_record(tmp_path):
    (tmp_path / "_execution").mkdir()
    (tmp_path / STAGED_PROVENANCE).write_text(json.dumps({"entries": [{"output": "a"}]}))
    parts_dir = tmp_path / PARTS_DIR
    parts_dir.mkdir(parents=True)
    (tmp_path / part_file("part-1", "image.provenance.json")).write_text(
        json.dumps({"entries": [{"output": "b"}]}))
    (tmp_path / part_file("part-2", "host.provenance.json")).write_text(
        json.dumps({"entries": [{"output": "c"}]}))
    (parts_dir / "part-1.json").write_text(json.dumps({"runs": [], "jobs": []}))
    assert sorted(e["output"] for e in _staged_provenance_entries(str(tmp_path))) == [
        "a", "b", "c"]


def test_the_parts_logs_are_read_in_order_with_a_header_each(tmp_path):
    parts = [ps.Part(name="part-1", runs=["a/0"]), ps.Part(name="part-2", runs=["a/1", "a/2"])]
    os.makedirs(tmp_path / PARTS_DIR)
    (tmp_path / part_file("part-1", "postprocessing.log")).write_text("zero\n")
    (tmp_path / part_file("part-2", "postprocessing.log")).write_text("one")
    log = ps.delivered_map_log(str(tmp_path), parts)
    assert log.index("part-1 (1 of 2, 1 run(s))") < log.index("zero") < log.index("part-2")
    assert log.endswith("one\n")


def test_a_part_that_delivered_no_log_is_named_rather_than_left_out(tmp_path):
    """The converter runs before the container that writes a part's log, so a part whose
    conversion failed delivers nothing. Dropping its block leaves the campaign's section
    reading as though that part never existed -- and it is the part being looked for."""
    parts = [ps.Part(name="part-1", runs=["a/0"]), ps.Part(name="part-2", runs=["a/1"])]
    os.makedirs(tmp_path / PARTS_DIR)
    (tmp_path / part_file("part-1", "postprocessing.log")).write_text("zero\n")

    log = ps.delivered_map_log(str(tmp_path), parts)

    assert "part-2" in log
    assert "delivered no log" in log


def test_a_failed_part_keeps_the_pod_s_own_log(tmp_path, monkeypatch):
    """The pods are still there when the verdict is taken and gone by the time the campaign's
    section is rebuilt from what was delivered, so this is the last moment the output of the
    container that actually failed can be read at all."""
    parts = [ps.Part(name="part-1", runs=["a/0"]), ps.Part(name="part-2", runs=["a/1"])]
    os.makedirs(tmp_path / PARTS_DIR)
    (tmp_path / part_file("part-1", "postprocessing.log")).write_text("part one is fine\n")
    monkeypatch.setattr(pj, "read_job_log", lambda *a, **k: "convert: no such handler\n")
    monkeypatch.setattr(pj, "pod_failure_reason", lambda *a, **k: "container convert exited 1")

    phase = ps._MapPhase.__new__(ps._MapPhase)
    phase.campaign_root = str(tmp_path)
    phase.parts, phase.names = parts, ["job-1", "job-2"]
    phase.core, phase.namespace, phase.admission = None, "ns", None
    phase.ever_created = {"job-1", "job-2"}
    phase.outcome = {"job-1": "succeeded", "job-2": "failed"}

    ok, message = phase.verdict()

    assert ok is False and "exited 1" in message
    kept = (tmp_path / part_file("part-2", "postprocessing.log")).read_text()
    assert kept == "convert: no such handler\n"
    assert (tmp_path / part_file("part-1", "postprocessing.log")).read_text() == (
        "part one is fine\n"), "a part's own account of itself is not overwritten"


# -- where the campaign is decided -------------------------------------------


def test_a_campaign_runs_in_one_job_when_the_cap_says_one(monkeypatch, tmp_path):
    monkeypatch.setenv(ps.MAX_PARALLEL_ENV, "1")
    (tmp_path / "_config").mkdir()
    vast = tmp_path / "_config" / "c.vast"
    vast.write_text("version: 1\n")
    assert pj._plan_split(str(tmp_path), str(vast)) is None  # pylint: disable=protected-access


def test_a_split_runs_the_map_then_the_reduce_and_keeps_the_parts_log(monkeypatch, tmp_path):
    parts = [ps.Part(name="part-1", runs=["a/0"]), ps.Part(name="part-2", runs=["a/1"])]
    monkeypatch.setattr(pj, "_read_submit_inputs",
                        lambda root, skip=None, skip_rosout=False:
                        ([], "img", (), None, (["run_log"], ["compress"], parts)))
    monkeypatch.setattr(pj, "campaign_vast", lambda root: str(tmp_path / "_config" / "c.vast"))
    calls = []

    def _map(*a, **k):
        calls.append("map")
        os.makedirs(tmp_path / PARTS_DIR, exist_ok=True)
        (tmp_path / part_file("part-1", "postprocessing.log")).write_text("part zero\n")
        return True, "2 part(s) complete"

    def _reduce(*a, **k):
        calls.append(("reduce", k["role"].skip_map, k["role"].stage_bags,
                      "part zero" in k["log_prefix"]))
        pj.write_phase_log(tmp_path, "reduce's own\n")
        return True, "done"

    monkeypatch.setattr(ps, "run_map_phase", _map)
    monkeypatch.setattr(pj, "run_conversion_job", _reduce)
    ok, _message = pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns", token="t")
    assert ok and calls == ["map", ("reduce", True, True, True)]
    log = (tmp_path / "_execution" / "postprocessing.log").read_text()
    assert log.index("part zero") < log.index("reduce's own")
    plan = ps.read_plan(str(tmp_path))
    assert plan["parts"] == ["part-1", "part-2"]


def test_a_failed_map_never_starts_the_reduce(monkeypatch, tmp_path):
    parts = [ps.Part(name="part-1", runs=["a/0"]), ps.Part(name="part-2", runs=["a/1"])]
    monkeypatch.setattr(pj, "_read_submit_inputs",
                        lambda root, skip=None, skip_rosout=False:
                        ([], "img", (), None, (["run_log"], [], parts)))
    monkeypatch.setattr(ps, "run_map_phase", lambda *a, **k: (False, "1 of 2 part(s) failed"))
    monkeypatch.setattr(pj, "run_conversion_job",
                        lambda *a, **k: pytest.fail("the reduce ran after a failed map"))
    ok, message = pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns", token="t")
    assert ok is False and "1 of 2 part(s) failed" in message


def test_the_deployment_carries_the_cap_and_refuses_a_bad_one(monkeypatch):
    from robovast.execution.cluster_execution import service_deploy
    monkeypatch.setattr(service_deploy, "_cluster_maximum", lambda *a: ("", ""))
    monkeypatch.setenv(ps.MAX_PARALLEL_ENV, "6")
    env = {e["name"]: e["value"] for e in service_deploy._cluster_env("ns", None, None)}  # pylint: disable=protected-access
    assert env[ps.MAX_PARALLEL_ENV] == "6"
    monkeypatch.setenv(ps.MAX_PARALLEL_ENV, "lots")
    with pytest.raises(ValueError, match=ps.MAX_PARALLEL_ENV):
        service_deploy._cluster_env("ns", None, None)  # pylint: disable=protected-access


# -- the map phase against a fake cluster -----------------------------------


class _Status:
    def __init__(self, active=None, failed=None, done=False):
        self.active, self.failed = active, failed
        self.completion_time = "t" if done else None


class _FakeBatch:
    """Jobs run for *rounds* listings, then end as *verdicts* says (default: succeed)."""

    def __init__(self, rounds=1, verdicts=None):
        self.created, self.listings, self.deleted = [], {}, []
        self.rounds, self.verdicts = rounds, verdicts or {}

    def create_namespaced_job(self, namespace, body):
        self.created.append(body["metadata"]["name"])

    def list_namespaced_job(self, namespace, label_selector):
        import types
        items = []
        for name in self.created:
            seen = self.listings[name] = self.listings.get(name, 0) + 1
            if seen <= self.rounds:
                status = _Status(active=1)
            elif self.verdicts.get(name) == "failed":
                status = _Status(failed=1)
            else:
                status = _Status(done=True)
            items.append(types.SimpleNamespace(metadata=types.SimpleNamespace(name=name),
                                               status=status))
        return types.SimpleNamespace(items=items)

    def delete_namespaced_job(self, name, namespace, body):
        self.deleted.append(name)


class _FakeCore:
    def list_namespaced_pod(self, namespace, label_selector):
        import types
        return types.SimpleNamespace(items=[])


@pytest.fixture
def cluster(monkeypatch, dsn, tmp_path):
    (tmp_path / "_config").mkdir()
    (tmp_path / "_config" / "c.vast").write_text("version: 1\n")
    batch, core = _FakeBatch(), _FakeCore()
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.load_kube_config",
                        lambda context=None, **kw: None)
    monkeypatch.setattr("kubernetes.client.BatchV1Api", lambda: batch)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)
    monkeypatch.setattr("robovast.execution.cluster_execution.cluster_execution."
                        "resolve_pull_secret", lambda *a: "")
    monkeypatch.setattr(ps.pod_access, "ensure_campaign_secret", lambda *a: None)
    monkeypatch.setattr(pj, "live_job", lambda *a: False)
    monkeypatch.setattr(pj, "POLL_SECONDS", 0)
    monkeypatch.setattr(pj, "pod_failure_reason", lambda core, ns, name: "exit 1")
    return batch, tmp_path


_PARTS = [ps.Part(name="part-1", runs=["a/0"], jobs=["_jobs/batch-0/job-0"]),
           ps.Part(name="part-2", runs=["a/1"], jobs=["_jobs/batch-0/job-1"])]


def test_every_part_runs_in_its_own_job_and_the_phase_waits_for_all(cluster):
    batch, root = cluster
    ok, message = ps.run_map_phase(object(), "camp", str(root), "ns", None, ["run_log"],
                                   _PARTS, token="t")
    assert ok is True, message
    assert batch.created == ps.part_job_names("camp", _PARTS)
    assert json.loads((root / PARTS_DIR / "part-2.json").read_text())["runs"] == ["a/1"]


def test_a_failed_part_fails_the_phase_and_names_itself(cluster):
    batch, root = cluster
    failing = ps.part_job_names("camp", _PARTS)[1]
    batch.verdicts = {failing: "failed"}
    ok, message = ps.run_map_phase(object(), "camp", str(root), "ns", None, ["run_log"],
                                   _PARTS, token="t")
    assert ok is False and failing in message and "exit 1" in message


def test_a_part_left_running_is_waited_for_not_created_again(cluster, monkeypatch):
    batch, root = cluster
    running = ps.part_job_names("camp", _PARTS)[0]
    monkeypatch.setattr(pj, "live_job", lambda b, c, ns, name: name == running)
    batch.created.append(running)          # it exists in the cluster already
    ok, _ = ps.run_map_phase(object(), "camp", str(root), "ns", None, ["run_log"], _PARTS,
                             token="t")
    assert ok is True
    assert batch.created.count(running) == 1


def test_a_stop_deletes_every_part(cluster):
    batch, root = cluster
    ok, message = ps.run_map_phase(object(), "camp", str(root), "ns", None, ["run_log"],
                                   _PARTS, token="t", should_stop=lambda: True)
    assert ok is False and "cancelled" in message
    assert sorted(batch.deleted) == sorted(ps.part_job_names("camp", _PARTS))


def test_image_steps_without_an_image_are_refused(cluster):
    _batch, root = cluster
    ok, message = ps.run_map_phase(
        object(), "camp", str(root), "ns", None,
        [{"rosbags_process": {"plugins": [{"type": "to_csv"}]}}], _PARTS, token="t")
    assert ok is False and "execution image" in message
