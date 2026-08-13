# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Creating a workspace from a campaign's frozen ``_config/``.

The way out of the read-only campaign-config view: the UI can *show* what a campaign ran, and this
is how that becomes something editable. Two things here fail silently and are what these tests are
for:

- **the scenario's path.** ``_config/`` archives the scenario at its basename while
  ``execution.scenario_file`` may declare a subdirectory, so a plain directory copy yields a
  workspace whose every validation reports "scenario file not found" — for a project the service
  itself produced. This is why the seeding reuses ``retrigger.reconstruct_project`` rather than
  copying, and the regression is `test_a_scenario_in_a_subdirectory...`.
- **a half-populated workspace.** A refusal must leave nothing registered, or a workspace named
  after a campaign sits in the picker looking like that campaign's project while being short the
  files it ran with.

The other half of the decision is pinned negatively: a campaign is *not* a workspace and never
appears in the workspace list. That id would otherwise be launchable — ``resolve()`` is the front
door for ``create_campaign`` — which would be a second retrigger path past every refusal
``retrigger.prepare`` exists to make.
"""

import pytest
import yaml

from robovast.service.interface import CreateWorkspaceRequest
from robovast.service.local_transport import LocalTransport
from robovast.service.workspaces import WorkspaceError, WorkspaceRegistry, WorkspaceStore

CAMPAIGN = "pilot-2026-08-08-120000"


def _vast(scenario="scenario.osc"):
    return {"version": 2, "metadata": {"name": "pilot"},
            "configuration": [{"name": "config1"}],
            "execution": {"scenario_file": scenario, "runs": 3,
                          "containers": {"scenario": {"image": "base:1"}}}}


def _source_campaign(root, campaign_id=CAMPAIGN, *, vast=None, run_files=(), extra_config=()):
    """A campaign directory with the frozen config a real run leaves behind."""
    campaign = root / campaign_id
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "pilot.vast").write_text(yaml.safe_dump(vast or _vast()))
    (campaign / "_config" / "scenario.osc").write_text("scenario pilot:\n")
    for rel in extra_config:
        path = campaign / "_config" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    (campaign / "_transient").mkdir(exist_ok=True)
    (campaign / "_transient" / "configurations.yaml").write_text(yaml.safe_dump(
        {"configs": [{"name": "config1"}], "_run_files": list(run_files)}))
    return campaign


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = LocalTransport(store=store)
    results = tmp_path / "results"
    results.mkdir()
    transport._campaigns_root = lambda: results        # noqa: SLF001
    return transport


def _create(svc, name="from-pilot", from_campaign=CAMPAIGN):
    return svc.create_workspace(
        CreateWorkspaceRequest(name=name, from_campaign=from_campaign))


def _project(svc, info):
    return svc.store.registry.project_dir(info.workspace_id)


# -- what lands in the workspace ---------------------------------------------------


def test_the_frozen_config_is_reproduced_in_the_new_workspace(svc, tmp_path):
    _source_campaign(tmp_path / "results", extra_config=("files/nav2_params.yaml",))
    project = _project(svc, _create(svc))
    assert (project / "pilot.vast").is_file()
    assert (project / "scenario.osc").is_file()
    # Nested, not flattened: a params directory the campaign carried is a directory here too.
    assert (project / "files" / "nav2_params.yaml").is_file()


def test_a_scenario_in_a_subdirectory_is_put_where_the_vast_says(svc, tmp_path):
    """The regression a plain copy would cause. ``_config/`` holds the scenario flat; config
    generation needs it at the declared path, and the ``.vast`` must not be rewritten to match
    because its config block feeds ``compute_config_identifier``."""
    campaign = _source_campaign(tmp_path / "results", vast=_vast("scenarios/pilot.osc"))
    (campaign / "_config" / "scenario.osc").rename(campaign / "_config" / "pilot.osc")

    project = _project(svc, _create(svc))
    assert (project / "scenarios" / "pilot.osc").is_file()
    written = yaml.safe_load((project / "pilot.vast").read_text())
    assert written["execution"]["scenario_file"] == "scenarios/pilot.osc"


def test_an_empty_workspace_is_still_empty(svc):
    """``from_campaign`` is opt-in: the ordinary create must not acquire a source."""
    info = svc.create_workspace(CreateWorkspaceRequest(name="plain"))
    assert list(_project(svc, info).rglob("*")) == []


# -- a refusal leaves nothing behind ----------------------------------------------


def test_a_campaign_with_no_frozen_config_is_refused(svc, tmp_path):
    (tmp_path / "results" / CAMPAIGN).mkdir(parents=True)
    with pytest.raises(ValueError) as e:
        _create(svc)
    assert "_config/" in str(e.value)
    assert svc.list_workspaces().workspaces == []


def test_an_unknown_campaign_is_refused(svc):
    with pytest.raises(ValueError):
        _create(svc, from_campaign="no-such-campaign")
    assert svc.list_workspaces().workspaces == []


def test_a_config_missing_a_recorded_run_file_is_refused(svc, tmp_path):
    """A workspace short a file the run used would name that campaign's configuration while
    running a different one -- the same silent failure a retrigger refuses, one step earlier."""
    _source_campaign(tmp_path / "results", run_files=("files/nav2_params.yaml",))
    with pytest.raises(ValueError) as e:
        _create(svc)
    assert "files/nav2_params.yaml" in str(e.value)
    # Nothing registered, and no orphaned project tree either.
    assert svc.list_workspaces().workspaces == []
    assert list(svc.store.registry.root.glob("ws-*")) == []


def test_a_refusal_does_not_consume_the_name(svc, tmp_path):
    """The follow-on of leaving nothing behind: ``create`` suffixes a colliding name, so a
    workspace left registered by a failed attempt would make the retry ``from-pilot-2``."""
    _source_campaign(tmp_path / "results", run_files=("files/nav2_params.yaml",))
    with pytest.raises(ValueError):
        _create(svc)
    campaign = tmp_path / "results" / CAMPAIGN
    (campaign / "_config" / "files").mkdir()
    (campaign / "_config" / "files" / "nav2_params.yaml").write_text("x")
    assert _create(svc).name == "from-pilot"


# -- the campaign is not a workspace ----------------------------------------------


def test_a_campaign_id_is_not_a_workspace(svc, tmp_path):
    """Pins the design decision, so a later "hidden workspace entry" for the same feature trips a
    test that says why: any id the registry resolves can be launched with ``create_campaign``."""
    _source_campaign(tmp_path / "results")
    for candidate in (CAMPAIGN, f"campaign:{CAMPAIGN}"):
        with pytest.raises(WorkspaceError):
            svc.store.registry.require(candidate)


def test_a_seeded_workspace_is_an_ordinary_writable_one(svc, tmp_path):
    """The point of seeding: what comes back is editable, unlike the snapshot it came from."""
    _source_campaign(tmp_path / "results")
    info = _create(svc)
    assert info.read_only is False
    assert [w.workspace_id for w in svc.list_workspaces().workspaces] == [info.workspace_id]
