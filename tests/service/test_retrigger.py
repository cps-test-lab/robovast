# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Relaunching a campaign from its own results.

Three things here can fail silently, and each has tests that would catch it:

- **the image.** A campaign's build context is not archived, so a retrigger cannot rebuild and
  must reuse the recorded ref. Falling back to a declared tag would run the base image without
  the campaign's own code — a campaign that finishes and measured nothing.
- **the config.** ``execution.run_files`` is a list of globs, and a glob matching nothing is
  only a *warning* during config generation. A ``_config/`` missing a params file would produce
  a campaign that runs, runs differently, and says so nowhere.
- **the launch.** ``config_filter`` is replayed from ``_execution/launch.yaml``; without it, one
  click on a one-config pilot becomes the full sweep.

Plus the staging directory, which is scratch that must not accumulate: every way a campaign can
end has to release it, including the ways that never reach a worker.
"""

import threading

import pytest
import yaml

from robovast.common.campaign_data import write_launch_record
from robovast.service import retrigger
from robovast.service.interface import CreateCampaignRequest
from robovast.service.local_transport import LocalTransport, WorkspaceTarget
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

DIGEST = "harbor.example/robovast/exp@sha256:" + "9" * 64


def _vast(containers=None):
    return {"version": 3, "metadata": {"name": "pilot"},
            "configuration": [{"name": "config1"}],
            "execution": {"scenario_file": "scenario.osc", "runs": 3,
                          "containers": containers or {"scenario": {"image": "base:1"}}}}


#: What a campaign records when it ran one container that built its own image. ``images`` is
#: written after ``apply_backend``, so its keys are the containers that actually ran — which is
#: why a ``.vast`` declaring only ``simulation`` can legitimately record ``scenario``.
BUILT = {"runs": 3, "execution_type": "cluster", "images": {"scenario": "build:pilot"},
         "image_revision": DIGEST}


def _source_campaign(root, campaign_id="pilot-2026-08-08-120000", *, vast=None,
                     execution=None, launch=None, run_files=(), extra_config=()):
    """A campaign directory shaped like one a real run leaves behind."""
    campaign = root / campaign_id
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "pilot.vast").write_text(yaml.safe_dump(vast or _vast()))
    (campaign / "_config" / "scenario.osc").write_text("scenario pilot:\n")
    for rel in extra_config:
        path = campaign / "_config" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    (campaign / "_execution").mkdir(exist_ok=True)
    (campaign / "_execution" / "execution.yaml").write_text(yaml.safe_dump(
        execution if execution is not None
        else {"runs": 3, "execution_type": "cluster", "image_revision": DIGEST}))
    (campaign / "_transient").mkdir(exist_ok=True)
    (campaign / "_transient" / "configurations.yaml").write_text(yaml.safe_dump(
        {"configs": [{"name": "config1"}], "_run_files": list(run_files)}))
    if launch is not None:
        write_launch_record(campaign, launch)
    return campaign


@pytest.fixture
def svc(tmp_path, monkeypatch):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = LocalTransport(store=store)
    results = tmp_path / "results"
    results.mkdir()
    transport._campaigns_root = lambda: results        # noqa: SLF001
    return transport


def _staged(svc):
    root = retrigger.staging_root(svc.store.registry.root)
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def _prepare(svc, campaign_id):
    return retrigger.prepare(
        svc._retrigger_source_dir(campaign_id), campaign_id,   # noqa: SLF001
        workspaces_root=svc.store.registry.root, description_limit=200,
        request_model=CreateCampaignRequest)


# -- the launch is replayed, not guessed -------------------------------------------


def test_a_piloted_campaign_is_retriggered_as_a_pilot(svc, tmp_path):
    """The reason ``launch.yaml`` exists: without it this filter is unrecoverable and the
    retrigger silently runs every configuration instead of the one that was piloted."""
    _source_campaign(tmp_path / "results", launch=CreateCampaignRequest(
        workspace_id="ws-gone", config_filter="config1*", runs=1))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.request.config_filter == "config1*"
    assert plan.request.runs == 1


def test_the_new_campaign_names_the_one_it_came_from(svc, tmp_path):
    _source_campaign(tmp_path / "results",
                     launch=CreateCampaignRequest(workspace_id="w"))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.request.description.startswith("retrigger of pilot-2026-08-08-120000")
    # A retrigger is nobody sitting at a screen, whatever the original asked for.
    assert plan.request.show_gui is False
    assert plan.request.workspace_id == ""


def test_a_campaign_predating_the_launch_record_says_the_filter_is_unknown(svc, tmp_path):
    """It runs everything, because the filter cannot be recovered — but it must not do that
    silently, since the source may have been a pilot."""
    _source_campaign(tmp_path / "results", execution={
        "runs": 7, "execution_type": "cluster", "image_revision": DIGEST})
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.request.config_filter == ""
    assert plan.request.runs == 7          # the effective count from execution.yaml
    assert "no launch record" in plan.request.description


# -- the image is pinned, never rebuilt --------------------------------------------


def test_a_built_container_is_pinned_to_the_recorded_image(svc, tmp_path):
    _source_campaign(tmp_path / "results", execution=BUILT, vast=_vast(
        {"scenario": {"image": "base:1", "python_packages": ["wheels/x.whl"]}}))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.pinned_images == {"scenario": DIGEST}


def test_a_folded_simulation_block_is_pinned_as_the_scenario_container(svc, tmp_path):
    """The shape of every stepped-simulator campaign in this repo.

    The ``.vast`` declares only ``simulation`` — with an image, so no declaration-side heuristic
    can tell it is not a separate container — and the campaign records ``scenario``. Taking the
    container set from the record is what makes this work without loading the simulator plugin.
    """
    _source_campaign(tmp_path / "results", execution=BUILT, vast=_vast(
        {"simulation": {"image": "base:1", "python_packages": ["wheels/x.whl"]}}))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.pinned_images == {"scenario": DIGEST}


def test_a_campaign_that_builds_nothing_pins_nothing(svc, tmp_path):
    """Its containers run their declared images, so there is nothing to pin and no refusal."""
    _source_campaign(tmp_path / "results")
    assert _prepare(svc, "pilot-2026-08-08-120000").pinned_images == {}


def test_a_built_container_with_no_recorded_image_is_refused(svc, tmp_path):
    """The common shape of a failed cluster campaign: ``_config/`` frozen, no execution.yaml.
    Rebuilding is not an option — the wheels are not in the results."""
    campaign = _source_campaign(tmp_path / "results", vast=_vast(
        {"scenario": {"image": "base:1", "python_packages": ["wheels/x.whl"]}}))
    (campaign / "_execution" / "execution.yaml").unlink()
    with pytest.raises(retrigger.RetriggerRefused) as e:
        _prepare(svc, "pilot-2026-08-08-120000")
    assert "build context" in str(e.value)
    assert e.value.include_traceback is False       # self-contained: no stack wanted


def test_a_build_free_campaign_with_only_tags_recorded_is_retriggered(svc, tmp_path):
    """The regression this whole change is about.

    A cluster campaign that built nothing records mutable tags in ``images`` and, if its
    per-container digests were lost (a resume whose pods were already reaped), nothing pinnable
    at all. It used to be refused for failing to pin images it never built — while the refusal
    told you to relaunch from the workspace, which resolves exactly the same tags. Now it
    proceeds and says what it re-resolved.
    """
    _source_campaign(
        tmp_path / "results",
        vast=_vast({"simulation": {"image": "sim:1"}, "sut": {"image": "sut:1"}}),
        execution={"runs": 3, "execution_type": "cluster",
                   "images": {"simulation": "reg.example/sim:latest",
                              "sut": "reg.example/sut:latest"}},
        launch=CreateCampaignRequest(workspace_id="ws-gone", runs=3))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.pinned_images == {}     # nothing pinned, and that is not an error


def test_a_build_free_campaign_is_not_blocked_by_the_preflight(svc, tmp_path):
    """``check`` and ``prepare`` must agree; they answered this differently once."""
    _source_campaign(
        tmp_path / "results",
        vast=_vast({"simulation": {"image": "sim:1"}, "sut": {"image": "sut:1"}}),
        execution={"runs": 3, "execution_type": "cluster",
                   "images": {"simulation": "reg.example/sim:latest",
                              "sut": "reg.example/sut:latest"}},
        launch=CreateCampaignRequest(workspace_id="ws-gone", runs=3))
    report = retrigger.check(
        svc._retrigger_source_dir("pilot-2026-08-08-120000"),   # noqa: SLF001
        "pilot-2026-08-08-120000")
    assert "images" not in report["blocking"]
    assert report["axes"]["images"]["reresolved"] == ["simulation", "sut"]


def test_a_built_container_that_recorded_only_a_tag_is_still_refused(svc, tmp_path):
    """The half that must not be lost: it BUILT this image, so a tag is not a substitute."""
    _source_campaign(
        tmp_path / "results",
        vast=_vast({"scenario": {"image": "base:1", "python_packages": ["wheels/x.whl"]}}),
        execution={"runs": 3, "execution_type": "cluster",
                   "images": {"scenario": "reg.example/exp:latest"}})
    with pytest.raises(retrigger.RetriggerRefused) as e:
        _prepare(svc, "pilot-2026-08-08-120000")
    assert "build context" in str(e.value)


def test_no_execution_yaml_is_fine_when_nothing_builds(svc, tmp_path):
    """Refusing here would rule out relaunching any campaign that died early, which is most
    of what someone wants to relaunch."""
    campaign = _source_campaign(tmp_path / "results")
    (campaign / "_execution" / "execution.yaml").unlink()
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.pinned_images == {}


def test_a_campaign_with_no_frozen_config_is_refused(svc, tmp_path):
    campaign = _source_campaign(tmp_path / "results")
    (campaign / "_config" / "pilot.vast").unlink()
    with pytest.raises(retrigger.RetriggerRefused) as e:
        _prepare(svc, "pilot-2026-08-08-120000")
    assert "_config/" in str(e.value)


# -- staging ----------------------------------------------------------------------


def test_staging_reproduces_the_run_files_at_their_recorded_paths(svc, tmp_path):
    _source_campaign(tmp_path / "results", run_files=("files/nav2_params.yaml",),
                     extra_config=("files/nav2_params.yaml",))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    plan.materialize()
    assert (plan.staging_dir / "files" / "nav2_params.yaml").is_file()
    assert (plan.staging_dir / "pilot.vast").is_file()


def test_a_file_ref_variations_module_is_reproduced_in_the_staging_tree(svc, tmp_path):
    """The reported failure, end to end: a retrigger re-composes, so it needs the module.

    A retrigger does not replay recorded configurations -- it hands the staged ``.vast`` back
    to composition, which resolves ``variations/doorway.py:DoorwayVariation`` against the
    staging directory. Once the module is collected as a run file it is archived into
    ``_config/`` and lands here at the path the reference names.
    """
    vast = _vast()
    vast["configuration"] = [
        {"name": "config1",
         "variations": [{"variations/doorway.py:DoorwayVariation": {}}]}]
    _source_campaign(tmp_path / "results", vast=vast,
                     run_files=("variations/doorway.py",),
                     extra_config=("variations/doorway.py",))

    plan = _prepare(svc, "pilot-2026-08-08-120000")
    plan.materialize()

    assert (plan.staging_dir / "variations" / "doorway.py").is_file()


def test_a_scenario_in_a_subdirectory_is_put_back_where_the_vast_says(svc, tmp_path):
    """``_config/`` flattens the scenario to its basename, but config generation requires it at
    the declared relative path — and the .vast must not be rewritten to match, because its
    config block feeds ``compute_config_identifier``."""
    vast = _vast()
    vast["execution"]["scenario_file"] = "scenarios/pilot.osc"
    campaign = _source_campaign(tmp_path / "results", vast=vast)
    (campaign / "_config" / "scenario.osc").rename(campaign / "_config" / "pilot.osc")

    plan = _prepare(svc, "pilot-2026-08-08-120000")
    plan.materialize()
    assert (plan.staging_dir / "scenarios" / "pilot.osc").is_file()
    staged_vast = yaml.safe_load((plan.staging_dir / "pilot.vast").read_text())
    assert staged_vast["execution"]["scenario_file"] == "scenarios/pilot.osc"


def test_a_config_missing_a_recorded_run_file_is_refused(svc, tmp_path):
    """The silent failure this check exists for: the glob would simply match nothing, warn, and
    produce a campaign that ran with different parameters."""
    _source_campaign(tmp_path / "results", run_files=("files/nav2_params.yaml",))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    with pytest.raises(retrigger.RetriggerRefused) as e:
        plan.materialize()
    assert "files/nav2_params.yaml" in str(e.value)


# -- the staging directory is scratch, and must not accumulate ---------------------


def test_the_staged_tree_is_released_when_the_campaign_ends(svc, tmp_path, monkeypatch):
    """Not on delete_campaign: nothing reads the tree once the campaign is terminal, and
    waiting for a delete would leak a pip target tree per launch."""
    _source_campaign(tmp_path / "results")
    done = threading.Event()
    monkeypatch.setattr(LocalTransport, "_build_specs_for",
                        lambda self, t, c: ({}, None))
    monkeypatch.setattr(LocalTransport, "_postprocess_in_process", lambda self: False)
    monkeypatch.setattr("robovast.execution.controller.run_batch_campaign",
                        lambda *a, **k: done.set())
    svc.retrigger_campaign("pilot-2026-08-08-120000")
    assert done.wait(5)
    for entry in list(svc._campaigns.values()):        # noqa: SLF001
        if entry.thread:
            entry.thread.join(5)
    assert _staged(svc) == []


def test_a_refused_launch_leaves_nothing_staged(svc, tmp_path, monkeypatch):
    """The most likely failure of all — the single-flight guard — happens before there is a
    worker, so the worker's ``finally`` cannot be what covers it."""
    _source_campaign(tmp_path / "results")
    monkeypatch.setattr(LocalTransport, "_guard_new_campaign",
                        lambda self: (_ for _ in ()).throw(RuntimeError("already running")))
    with pytest.raises(RuntimeError):
        svc.retrigger_campaign("pilot-2026-08-08-120000")
    assert _staged(svc) == []


def test_a_failure_inside_materialize_releases_the_tree(svc, tmp_path, monkeypatch):
    _source_campaign(tmp_path / "results", run_files=("files/missing.yaml",))
    monkeypatch.setattr(LocalTransport, "_postprocess_in_process", lambda self: False)
    svc.retrigger_campaign("pilot-2026-08-08-120000")
    for entry in list(svc._campaigns.values()):        # noqa: SLF001
        if entry.thread:
            entry.thread.join(5)
    assert _staged(svc) == []


def test_the_sweep_collects_a_tree_whose_campaign_is_finished(svc, tmp_path):
    results = tmp_path / "results"
    _source_campaign(results, "gone-2026-08-08-120000")
    (results / "gone-2026-08-08-120000" / "_execution" / "outcome.json").write_text("{}")
    root = retrigger.staging_root(svc.store.registry.root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "gone-2026-08-08-120000-abc123").mkdir()
    (root / "vanished-2026-08-08-130000-def456").mkdir()   # no campaign dir at all

    assert retrigger.sweep_orphans(svc.store.registry.root, results) == 2
    assert _staged(svc) == []


def test_the_sweep_leaves_a_tree_whose_campaign_looks_live(svc, tmp_path):
    """Two ``vast serve`` processes can share one workspaces root, so a tree whose campaign has
    not reached a terminal outcome may still be in use by the other one."""
    results = tmp_path / "results"
    _source_campaign(results, "live-2026-08-08-120000")    # no outcome.json
    root = retrigger.staging_root(svc.store.registry.root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "live-2026-08-08-120000-abc123").mkdir()

    assert retrigger.sweep_orphans(svc.store.registry.root, results) == 0
    assert _staged(svc) == ["live-2026-08-08-120000-abc123"]


def test_the_staging_root_cannot_be_mistaken_for_a_workspace_project(svc, tmp_path):
    """It sits under the workspaces root, so a dot name is what keeps ``_project_for_workspace``
    (which skips dot components) from ever resolving a staged copy as a project to run."""
    assert retrigger.staging_root(svc.store.registry.root).name.startswith(".")


# -- the launch path does not build when the image is pinned -----------------------


def test_a_pinned_launch_skips_the_build_and_uses_the_recorded_images(svc, tmp_path,
                                                                     monkeypatch):
    _source_campaign(tmp_path / "results", execution=BUILT, vast=_vast(
        {"scenario": {"image": "base:1", "python_packages": ["wheels/x.whl"]}}))
    started, used = [], {}
    monkeypatch.setattr(LocalTransport, "_start_build_images",
                        lambda self, t, c: started.append(1) or [])
    monkeypatch.setattr(LocalTransport, "_build_specs_for",
                        lambda self, t, c: ({}, None))
    monkeypatch.setattr(LocalTransport, "_postprocess_in_process", lambda self: False)
    monkeypatch.setattr("robovast.execution.controller.run_batch_campaign",
                        lambda *a, **k: used.update(k["options"].images or {}))

    svc.retrigger_campaign("pilot-2026-08-08-120000")
    for entry in list(svc._campaigns.values()):        # noqa: SLF001
        if entry.thread:
            entry.thread.join(5)
    assert started == []                    # nothing was built
    assert used == {"scenario": DIGEST}     # the recorded bytes ran


def test_a_workspace_launch_is_unaffected(svc):
    """The split must leave the ordinary path byte-for-byte equivalent."""
    target = WorkspaceTarget(config_path="/x/p.vast")
    assert target.materialize is None and target.discard is None
    assert target.pinned_images is None
