# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What ``vast workspace run`` puts in the ``CreateCampaignRequest``, and what it refuses.

The wiring is the point, not the transport: a wrong ``runs`` reaches the service as a
smaller campaign that still finishes green, and a description only the request model
rejects fails *after* the launch has been accepted.

``config_path`` is the field this command exists for. The launcher it replaced always left
it empty and relied on the push having pruned every other ``.vast`` from the workspace, so
a workspace holding several projects could not be launched at all.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import cli as root_cli


class _Ref:
    def __init__(self, campaign_id="camp-1"):
        self.campaign_id = campaign_id
        self.note = ""


class _Workspace:
    def __init__(self, workspace_id, name):
        self.workspace_id = workspace_id
        self.name = name
        self.running_campaigns = []


class _Client:
    """A service that records the campaign request and nothing else."""

    def __init__(self):
        self.request = None
        self.created = []
        self.workspaces = [_Workspace("ws-1", "my-experiment")]

    def list_workspaces(self):
        class _R:
            pass
        r = _R()
        r.workspaces = self.workspaces
        return r

    def create_workspace(self, request):
        ws = _Workspace(f"ws-{len(self.workspaces) + 1}", request.name)
        self.workspaces.append(ws)
        self.created.append(request.name)
        return ws

    def create_campaign(self, request):
        self.request = request
        return _Ref()


@pytest.fixture
def client(monkeypatch):
    service = _Client()

    @contextlib.contextmanager
    def _cm(*_a, **_k):
        yield service, "test target"

    # Patched where the name is *bound*: cli.py imports service_client at module level,
    # so rebinding the source module would not be seen.
    monkeypatch.setattr("robovast.client.cli.service_client", _cm)
    return service


def _invoke(*args):
    return CliRunner().invoke(root_cli.workspace, ['run', *args], catch_exceptions=False)


def test_the_vast_path_reaches_config_path(client):
    result = _invoke('my-experiment', 'sweeps/big.vast')
    assert result.exit_code == 0, result.output
    assert client.request.workspace_id == "ws-1"
    assert client.request.config_path == "sweeps/big.vast"


def test_omitting_the_path_lets_the_service_resolve_the_only_vast(client):
    result = _invoke('my-experiment')
    assert result.exit_code == 0, result.output
    # "" is the service's own "the one .vast", not a guess made here.
    assert client.request.config_path == ""


def test_a_workspace_name_resolves_to_its_id(client):
    client.workspaces = [_Workspace("ws-9", "other")]
    result = _invoke('other', 'a.vast')
    assert result.exit_code == 0, result.output
    assert client.request.workspace_id == "ws-9"


def test_runs_defaults_to_the_vast_not_one(client):
    result = _invoke('my-experiment')
    assert result.exit_code == 0, result.output
    # 0, not 1: the service reads a non-positive count as "use execution.runs". A
    # substitute for "unset" would shrink the campaign without failing anything.
    assert client.request.runs == 0


def test_explicit_runs_is_forwarded(client):
    _invoke('my-experiment', '--runs', '7')
    assert client.request.runs == 7


def test_filter_and_description_are_forwarded(client):
    _invoke('my-experiment', '--filter', 'hall*', '--description', 'pilot: 5 reps')
    assert client.request.config_filter == "hall*"
    assert client.request.description == "pilot: 5 reps"


def test_show_gui_reaches_the_request(client):
    _invoke('my-experiment', '--show-gui')
    assert client.request.show_gui is True


def test_an_over_long_description_is_refused_before_the_launch(client):
    from robovast.service.interface import DESCRIPTION_MAX_LEN

    result = _invoke('my-experiment', '--description', 'x' * (DESCRIPTION_MAX_LEN + 1))
    assert result.exit_code != 0
    assert "shorten it" in result.output
    # Nothing was launched: the check runs before the request is built.
    assert client.request is None


def test_campaign_id_is_not_accepted(client):
    # The service names the campaign; CreateCampaignRequest carries no id, so nothing
    # could honour one.
    result = CliRunner().invoke(root_cli.workspace,
                                ['run', 'my-experiment', '--campaign-id', 'x'])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_log_tree_is_not_accepted(client):
    # It was in the old launcher's signature and never read; CreateCampaignRequest has
    # no such field, so it was silently accepted and ignored.
    result = CliRunner().invoke(root_cli.workspace,
                                ['run', 'my-experiment', '--log-tree'])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_push_syncs_then_launches(client, monkeypatch, tmp_path):
    pushed = {}

    def fake_sync(_client, workspace_id, directory, **kwargs):  # noqa: ARG001
        pushed["workspace_id"] = workspace_id
        pushed["directory"] = str(directory)
        return {"written": 1, "uploaded": 0}

    monkeypatch.setattr(
        "robovast.service.project_push.sync_directory_to_workspace", fake_sync)
    project = tmp_path / "proj"
    project.mkdir()

    result = _invoke('my-experiment', 'a.vast', '--push', str(project))
    assert result.exit_code == 0, result.output
    assert pushed == {"workspace_id": "ws-1", "directory": str(project)}
    # And the launch still happened, with the same workspace.
    assert client.request.workspace_id == "ws-1"


def test_push_creates_a_workspace_that_does_not_exist_yet(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "robovast.service.project_push.sync_directory_to_workspace",
        lambda *a, **k: {"written": 0, "uploaded": 0})
    project = tmp_path / "proj"
    project.mkdir()

    result = _invoke('brand-new', 'a.vast', '--push', str(project))
    assert result.exit_code == 0, result.output
    assert client.created == ["brand-new"]


def test_push_never_claims_a_ws_id_as_a_new_name(client, monkeypatch, tmp_path):
    # An unknown ``ws-…`` is a typo, not a name to claim. ``_resolve_workspace_id``
    # passes any ``ws-`` prefix through without checking — the service is the authority
    # on whether it exists — so what must hold here is that nothing is *created* for it.
    monkeypatch.setattr(
        "robovast.service.project_push.sync_directory_to_workspace",
        lambda *a, **k: {"written": 0, "uploaded": 0})
    project = tmp_path / "proj"
    project.mkdir()

    _invoke('ws-does-not-exist', 'a.vast', '--push', str(project))
    assert client.created == []
    assert client.request.workspace_id == "ws-does-not-exist"


# -- validate / preview: the two steps that come before a launch --------------


class _CheckingClient(_Client):
    """Adds the two pre-launch reads to the recording client above."""

    def __init__(self, report=None, preview=None):
        super().__init__()
        self.report = report
        self.preview = preview
        self.asked = {}

    def validate_project(self, workspace_id, path="", check_world=True):
        self.asked = {"workspace_id": workspace_id, "path": path,
                      "check_world": check_world}
        return self.report

    def preview_configurations(self, workspace_id, max_configs=0, path=""):
        self.asked = {"workspace_id": workspace_id, "max_configs": max_configs,
                      "path": path}
        return self.preview


@pytest.fixture
def checking(monkeypatch):
    holder = {}

    @contextlib.contextmanager
    def _cm(*_a, **_k):
        yield holder["service"], "test target"

    monkeypatch.setattr("robovast.client.cli.service_client", _cm)
    return holder


def _report(valid, problems=()):
    from robovast.service.interface import ValidationProblem, ValidationReport
    return ValidationReport(
        valid=valid, configs=2, runs_per_config=3, total_trials=6,
        problems=[ValidationProblem(**p) for p in problems])


def test_validate_reports_every_problem_not_just_the_first(checking):
    checking["service"] = _CheckingClient(report=_report(False, [
        {"stage": "schema", "config": "hall-1", "field": "speed",
         "message": "must be a number"},
        {"stage": "scenario", "message": "no such action 'drive'"},
    ]))
    result = CliRunner().invoke(root_cli.workspace, ['validate', 'my-experiment'])
    assert result.exit_code != 0
    # Both, because they fail independently and fixing them one launch at a time is
    # the expensive way to find out.
    assert "must be a number" in result.output
    assert "no such action" in result.output
    assert "hall-1 speed" in result.output


def test_validate_reports_the_trial_count_when_it_passes(checking):
    checking["service"] = _CheckingClient(report=_report(True))
    result = CliRunner().invoke(root_cli.workspace,
                                ['validate', 'my-experiment', 'a.vast'])
    assert result.exit_code == 0, result.output
    assert "6 trial(s)" in result.output
    assert checking["service"].asked["path"] == "a.vast"


def test_no_world_check_is_forwarded(checking):
    checking["service"] = _CheckingClient(report=_report(True))
    CliRunner().invoke(root_cli.workspace,
                       ['validate', 'my-experiment', '--no-world-check'])
    assert checking["service"].asked["check_world"] is False


def test_preview_lists_the_configurations_and_the_trial_count(checking):
    from robovast.service.interface import PreviewConfiguration, PreviewResponse
    checking["service"] = _CheckingClient(preview=PreviewResponse(
        configs=2, runs_per_config=5, total_trials=10,
        configurations=[PreviewConfiguration(name="hall-1"),
                        PreviewConfiguration(name="hall-2")]))
    result = CliRunner().invoke(root_cli.workspace, ['preview', 'my-experiment'])
    assert result.exit_code == 0, result.output
    assert "hall-1" in result.output and "hall-2" in result.output
    assert "10 trial(s)" in result.output


def test_preview_says_when_it_truncated(checking):
    from robovast.service.interface import PreviewConfiguration, PreviewResponse
    checking["service"] = _CheckingClient(preview=PreviewResponse(
        configs=99, runs_per_config=1, total_trials=99, truncated=True,
        configurations=[PreviewConfiguration(name="hall-1")]))
    result = CliRunner().invoke(root_cli.workspace,
                                ['preview', 'my-experiment', '--max-configs', '1'])
    assert result.exit_code == 0, result.output
    # A silent cap reads as "that is all there is", which is the one thing it is not.
    assert "max-configs" in result.output
