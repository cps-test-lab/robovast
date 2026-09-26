# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Relaunching a campaign from its own results.

Three things here can fail silently, and each has tests that would catch it:

- **the images.** A retrigger runs exactly the digests the source's launch record holds -- every
  container, the sidecar and the aux helpers -- and resolves none of them again. A tag resolved
  at re-run time would run whatever was pushed there since, and a record missing a digest is
  refused, naming it, rather than filled in from the environment.
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

from robovast.common.campaign_data import LaunchImages, write_launch_record
from robovast.service import retrigger
from robovast.service.interface import CreateCampaignRequest
from robovast.service.service_base import WorkspaceTarget
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService

DIGEST = "harbor.example/robovast/exp@sha256:" + "9" * 64
SIDECAR = "harbor.example/robovast/robovast-sidecar@sha256:" + "5" * 64
AUX = "harbor.example/robovast/robovast-roqsim@sha256:" + "6" * 64
#: What a campaign launched now records: every image it runs, as a digest.
PINS = LaunchImages(containers={"scenario": DIGEST}, sidecar=SIDECAR,
                    aux={"aux-robovast-roqsim": AUX})
#: ``launch=`` default: a launch record from an ordinary request.
_ASKED = object()


def _vast(containers=None):
    return {"version": 6, "metadata": {"name": "pilot"},
            "configuration": [{"name": "config1"}],
            "execution": {"scenario_file": "scenario.osc", "runs": 3,
                          "containers": containers or {"scenario": {"image": "base:1"}}}}


#: What a campaign records when it ran one container that built its own image. ``images`` is
#: written after ``apply_backend``, so its keys are the containers that actually ran — which is
#: why a ``.vast`` declaring only ``simulation`` can legitimately record ``scenario``.
BUILT = {"runs": 3, "execution_type": "cluster", "images": {"scenario": "build:pilot"},
         "image_revision": DIGEST}


def _source_campaign(root, campaign_id="pilot-2026-08-08-120000", *, vast=None,
                     execution=None, launch=_ASKED, images=PINS, run_files=(), extra_config=(),
                     recorded=None):
    """A campaign directory shaped like one a real run leaves behind.

    *launch* is the request its launch record holds, ``None`` for no launch record at all;
    *images* the digests that record fixes.
    """
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
        {"configs": [{"name": "config1"}], "_run_files": list(run_files), **(recorded or {})}))
    if launch is _ASKED:
        launch = CreateCampaignRequest(workspace_id="ws-gone")
    if launch is not None:
        write_launch_record(campaign, launch, images=images)
    return campaign


@pytest.fixture
def svc(tmp_path, monkeypatch):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = NullService(store=store)
    results = tmp_path / "results"
    results.mkdir()
    transport._campaigns_root = lambda: results        # noqa: SLF001
    return transport


def _staged(svc):
    root = retrigger.staging_root(svc.store.registry.root)
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def _prepare(svc, campaign_id):
    return retrigger.prepare(
        str(svc.campaign_dir(campaign_id)), campaign_id,   # noqa: SLF001
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
    assert plan.request.workspace_id == ""


def test_a_campaign_predating_the_launch_record_is_refused(svc, tmp_path):
    """No launch record, so nothing fixes which bytes it ran -- a re-run would have to resolve
    every image again, which is a different experiment under the source's name."""
    _source_campaign(tmp_path / "results", launch=None, execution={
        "runs": 7, "execution_type": "cluster", "image_revision": DIGEST})
    with pytest.raises(retrigger.RetriggerRefused) as e:
        _prepare(svc, "pilot-2026-08-08-120000")
    assert "launch.yaml" in str(e.value)
    assert "--to-workspace" in str(e.value)


# -- every image is replayed, never resolved again ---------------------------------


def test_every_recorded_digest_is_replayed(svc, tmp_path):
    """Containers, the sidecar and the aux helpers: the plan carries all of them."""
    _source_campaign(tmp_path / "results", execution=BUILT, vast=_vast(
        {"scenario": {"image": "base:1", "python_packages": ["wheels/x.whl"]}}))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.pinned_images == PINS


def test_a_folded_simulation_block_is_pinned_as_the_scenario_container(svc, tmp_path):
    """The shape of every stepped-simulator campaign in this repo.

    The ``.vast`` declares only ``simulation`` — with an image, so no declaration-side heuristic
    can tell it is not a separate container — and the campaign records ``scenario``. Taking the
    container set from the record is what makes this work without loading the simulator plugin.
    """
    _source_campaign(tmp_path / "results", execution=BUILT, vast=_vast(
        {"simulation": {"image": "base:1", "python_packages": ["wheels/x.whl"]}}))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.pinned_images.containers == {"scenario": DIGEST}


def test_a_campaign_that_builds_nothing_replays_its_digests_too(svc, tmp_path):
    """Whether the campaign built its images makes no difference: a declared tag resolved
    again is as much a different experiment as a rebuilt image."""
    _source_campaign(tmp_path / "results")
    assert _prepare(svc, "pilot-2026-08-08-120000").pinned_images == PINS


def test_a_record_lacking_the_sidecar_is_refused_naming_it(svc, tmp_path):
    """The shape of a campaign launched before its record held every image."""
    _source_campaign(tmp_path / "results", images=LaunchImages(containers={"scenario": DIGEST}))
    with pytest.raises(retrigger.RetriggerRefused) as e:
        _prepare(svc, "pilot-2026-08-08-120000")
    assert "the sidecar image" in str(e.value)
    assert "--to-workspace" in str(e.value)
    assert e.value.include_traceback is False       # self-contained: no stack wanted


def test_a_build_free_campaign_with_only_tags_recorded_is_refused(svc, tmp_path):
    """Its containers ran tags nothing fixed, so a re-run has nothing to replay -- resolving
    them now is a fresh launch, which is what the refusal points at."""
    _source_campaign(
        tmp_path / "results",
        vast=_vast({"simulation": {"image": "sim:1"}, "sut": {"image": "sut:1"}}),
        execution={"runs": 3, "execution_type": "cluster",
                   "images": {"simulation": "reg.example/sim:latest",
                              "sut": "reg.example/sut:latest"}},
        launch=CreateCampaignRequest(workspace_id="ws-gone", runs=3),
        images=LaunchImages(sidecar=SIDECAR))
    with pytest.raises(retrigger.RetriggerRefused) as e:
        _prepare(svc, "pilot-2026-08-08-120000")
    assert "container 'simulation'" in str(e.value)
    assert "container 'sut'" in str(e.value)


def test_the_preflight_blocks_what_prepare_refuses(svc, tmp_path):
    """``check`` and ``prepare`` must agree; they read one record through one function."""
    _source_campaign(
        tmp_path / "results",
        vast=_vast({"simulation": {"image": "sim:1"}, "sut": {"image": "sut:1"}}),
        execution={"runs": 3, "execution_type": "cluster",
                   "images": {"simulation": "reg.example/sim:latest",
                              "sut": "reg.example/sut:latest"}},
        launch=CreateCampaignRequest(workspace_id="ws-gone", runs=3),
        images=LaunchImages(sidecar=SIDECAR))
    report = retrigger.check(
        str(svc.campaign_dir("pilot-2026-08-08-120000")),   # noqa: SLF001
        "pilot-2026-08-08-120000", image_labels=svc._image_labels,  # noqa: SLF001
        build_lock=svc._image_build_lock)  # noqa: SLF001
    assert "images" in report["blocking"]
    assert sorted(report["axes"]["images"]["missing"]) == ["container 'simulation'",
                                                           "container 'sut'"]
    assert "--to-workspace" in report["axes"]["images"]["detail"]


def test_a_container_that_recorded_only_a_tag_is_refused(svc, tmp_path):
    _source_campaign(
        tmp_path / "results",
        vast=_vast({"scenario": {"image": "base:1", "python_packages": ["wheels/x.whl"]}}),
        images=LaunchImages(containers={"scenario": "reg.example/exp:latest"},
                            sidecar=SIDECAR))
    with pytest.raises(retrigger.RetriggerRefused) as e:
        _prepare(svc, "pilot-2026-08-08-120000")
    assert "reg.example/exp:latest" in str(e.value)


def test_no_execution_yaml_is_fine_when_the_launch_record_is_complete(svc, tmp_path):
    """The launch record is written before the first job, so a campaign that died before its
    first batch -- most of what someone wants to relaunch -- is replayable from it alone."""
    campaign = _source_campaign(tmp_path / "results")
    (campaign / "_execution" / "execution.yaml").unlink()
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert plan.pinned_images == PINS


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


_SUT_EXECUTION = {"containers": {"sut": {"image": "sut:1",
                                         "config_files": {"nav2": "files/nav2_params.yaml"}}}}


def test_a_config_missing_a_declared_sut_source_is_refused(svc, tmp_path):
    """A `sut:` source is not in `_run_files` -- a run mounts only its cell's rewritten copy --
    but composition reads the original, so a snapshot without it cannot be composed again."""
    _source_campaign(tmp_path / "results", recorded={"execution": _SUT_EXECUTION})
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    with pytest.raises(retrigger.RetriggerRefused) as e:
        plan.materialize()
    assert "files/nav2_params.yaml" in str(e.value)


def test_an_archived_sut_source_satisfies_the_check(svc, tmp_path):
    source = _source_campaign(tmp_path / "results", recorded={"execution": _SUT_EXECUTION},
                              extra_config=("files/nav2_params.yaml",))
    assert retrigger.missing_run_files(source, source / "_config") == []


# -- the pre-flight refuses the launch, whichever client asked ---------------------


@pytest.fixture
def image_outside_the_window(monkeypatch):
    """Make the recorded image report a protocol version this host cannot drive.

    The host axis is the one the launch itself does not re-derive -- ``prepare`` pins the
    recorded ref and would hand it to a backend that cannot run it -- so it is what a gate
    on the operation has to catch.
    """
    from robovast.common import execution
    from robovast.common.execution import COMPAT_VERSION_LABEL

    monkeypatch.setattr(NullService, "_image_labels",
                        lambda self, ref: {COMPAT_VERSION_LABEL: "1"})
    monkeypatch.setattr(
        execution, "check_image_compat",
        lambda image, version=None, source="", unreadable=False:
            f"{image} speaks container protocol 1, outside this host's window")


def test_a_blocked_preflight_refuses_the_launch_and_names_the_axis(svc, tmp_path,
                                                                   image_outside_the_window):
    """The gate is on the operation, so a client that never checked cannot start a campaign
    that can only fail in the backend. Its message carries the axis's own detail, which is
    the actionable half."""
    _source_campaign(tmp_path / "results", execution=BUILT)
    with pytest.raises(retrigger.RetriggerRefused) as e:
        svc.retrigger_campaign("pilot-2026-08-08-120000")
    assert "host" in str(e.value)
    assert "outside this host's window" in str(e.value)
    assert "--force" in str(e.value)


def test_a_refused_preflight_stages_nothing(svc, tmp_path, image_outside_the_window):
    """It refuses before ``prepare``, so there is no tree to release -- and the refusal costs
    a few record reads rather than a directory."""
    _source_campaign(tmp_path / "results", execution=BUILT)
    with pytest.raises(retrigger.RetriggerRefused):
        svc.retrigger_campaign("pilot-2026-08-08-120000")
    assert _staged(svc) == []


def test_force_launches_past_a_blocking_axis(svc, tmp_path, monkeypatch,
                                             image_outside_the_window):
    """The argument is honoured rather than advisory: an axis the caller has decided they
    understand is theirs to override, and it is the only way past."""
    _source_campaign(tmp_path / "results", execution=BUILT)
    monkeypatch.setattr(NullService, "_build_specs_for", lambda self, t, c, **kw: ({}, None))
    monkeypatch.setattr("robovast.execution.controller.run_batch_campaign",
                        lambda *a, **k: None)

    ref = svc.retrigger_campaign("pilot-2026-08-08-120000", force=True)
    assert ref.campaign_id
    for entry in list(svc._campaigns.values()):        # noqa: SLF001
        if entry.thread:
            entry.thread.join(5)


def test_a_runnable_campaign_is_not_gated(svc, tmp_path, monkeypatch):
    """The pre-flight blocks; ``unknown`` and ``upgradable`` do not. A campaign recorded
    before a field existed is the case the whole pre-flight exists to rescue, so it must
    still launch."""
    _source_campaign(tmp_path / "results", execution=BUILT)
    monkeypatch.setattr(NullService, "_build_specs_for", lambda self, t, c, **kw: ({}, None))
    monkeypatch.setattr("robovast.execution.controller.run_batch_campaign",
                        lambda *a, **k: None)

    assert svc.retrigger_campaign("pilot-2026-08-08-120000").campaign_id
    for entry in list(svc._campaigns.values()):        # noqa: SLF001
        if entry.thread:
            entry.thread.join(5)


# -- the staging directory is scratch, and must not accumulate ---------------------


def test_the_staged_tree_is_released_when_the_campaign_ends(svc, tmp_path, monkeypatch):
    """Not on delete_campaign: nothing reads the tree once the campaign is terminal, and
    waiting for a delete would leak a pip target tree per launch."""
    _source_campaign(tmp_path / "results")
    done = threading.Event()
    monkeypatch.setattr(NullService, "_build_specs_for",
                        lambda self, t, c, **kw: ({}, None))
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
    monkeypatch.setattr(NullService, "_guard_new_campaign",
                        lambda self: (_ for _ in ()).throw(RuntimeError("already running")))
    with pytest.raises(RuntimeError):
        svc.retrigger_campaign("pilot-2026-08-08-120000")
    assert _staged(svc) == []


def test_a_failure_inside_materialize_releases_the_tree(svc, tmp_path, monkeypatch):
    _source_campaign(tmp_path / "results", run_files=("files/missing.yaml",))
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
    monkeypatch.setattr(NullService, "_start_build_images",
                        lambda self, t, c, **kw: started.append(1) or [])
    monkeypatch.setattr(NullService, "_build_specs_for",
                        lambda self, t, c, **kw: ({}, None))
    monkeypatch.setattr("robovast.execution.controller.run_batch_campaign",
                        lambda *a, **k: used.update(k["options"].images or {}))

    svc.retrigger_campaign("pilot-2026-08-08-120000")
    for entry in list(svc._campaigns.values()):        # noqa: SLF001
        if entry.thread:
            entry.thread.join(5)
    assert started == []                    # nothing was built
    assert used == {"scenario": DIGEST}     # the recorded bytes ran


def test_a_retrigger_replays_every_digest_whatever_the_environment_says(svc, tmp_path,
                                                                         monkeypatch):
    """The requirement end to end: the project and its tag moved after the source ran, and the
    re-run still runs the source's bytes -- containers, sidecar and aux helpers -- with every
    one of them fixed, so nothing downstream may resolve an image from the environment."""
    _source_campaign(tmp_path / "results")
    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/elsewhere")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "moved-on")
    seen = {}
    monkeypatch.setattr(NullService, "_build_specs_for",
                        lambda self, t, c, **kw: ({}, None))
    monkeypatch.setattr("robovast.execution.controller.run_batch_campaign",
                        lambda *a, **k: seen.update(options=k["options"]))

    new = svc.retrigger_campaign("pilot-2026-08-08-120000")
    for entry in list(svc._campaigns.values()):        # noqa: SLF001
        if entry.thread:
            entry.thread.join(5)

    options = seen["options"]
    assert options.images_fixed is True
    assert options.images == {"scenario": DIGEST}
    assert options.sidecar_image == SIDECAR
    assert options.aux_images == {"aux-robovast-roqsim": AUX}
    # And the new campaign's own record states them from its first write, so a re-run of the
    # re-run replays the same bytes again.
    from robovast.common.campaign_data import campaign_pinned_images
    assert campaign_pinned_images(svc.campaign_dir(new.campaign_id)) == PINS


def test_a_workspace_launch_is_unaffected(svc):
    """The split must leave the ordinary path byte-for-byte equivalent."""
    target = WorkspaceTarget(config_path="/x/p.vast")
    assert target.materialize is None and target.discard is None
    assert target.pinned_images is None


# -- keys a campaign ran without ---------------------------------------------------

_POOL_KEYS_VAST = """\
version: 6
metadata: {name: pilot}
configuration:
- name: config1
execution:
  scenario_file: scenario.osc
  runs: 3
  containers:
    scenario: {image: 'base:1'}  # the image the pilot ran
  kubernetes:
    jobs:
      node_labels: {pool: bench}
"""


def _archived_with_pool_keys(tmp_path, text=_POOL_KEYS_VAST):
    campaign = _source_campaign(tmp_path / "results",
                                execution={"runs": 3, "execution_type": "cluster"})
    (campaign / "_config" / "pilot.vast").write_text(text)
    return campaign


def test_a_campaign_carrying_the_pool_keys_is_retriggered_without_them(svc, tmp_path):
    """The staged copy is launched strictly, which refuses the keys by name; they never reached
    the source run, so they go, and the author's comments stay."""
    from robovast.common.common import load_config

    _archived_with_pool_keys(tmp_path)
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    staged = (plan.staging_dir / "pilot.vast").read_text()
    assert "node_labels" not in staged and "kubernetes" not in staged
    assert "# the image the pilot ran" in staged
    load_config(plan.config_path)                      # strict: the launch path
    archived = tmp_path / "results" / "pilot-2026-08-08-120000" / "_config" / "pilot.vast"
    assert "node_labels" in archived.read_text()


def test_a_pinned_campaign_is_retriggered_pinned(svc, tmp_path):
    from robovast.common.common import load_config
    from robovast.common.execution import job_node_alias

    _archived_with_pool_keys(tmp_path, _POOL_KEYS_VAST.replace(
        "      node_labels: {pool: bench}\n", "      node: bench-a\n"))
    plan = _prepare(svc, "pilot-2026-08-08-120000")
    assert job_node_alias(load_config(plan.config_path)) == "bench-a"


def test_a_workspace_seeded_from_such_a_campaign_is_launchable(svc, tmp_path):
    from robovast.common.common import load_config

    _archived_with_pool_keys(tmp_path)
    workspace_id = svc.store.registry.create("seeded")["workspace_id"]
    svc._seed_from_campaign(workspace_id, "pilot-2026-08-08-120000")   # noqa: SLF001
    seeded = svc.store.registry.project_dir(workspace_id) / "pilot.vast"
    assert "node_labels" not in seeded.read_text()
    load_config(str(seeded))
