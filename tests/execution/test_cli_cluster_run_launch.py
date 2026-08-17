# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What ``vast exec cluster run`` hands to the launch, and what it refuses first.

The wiring is the point here, not the push: a wrong ``runs`` reaches the service as a
smaller campaign that still finishes green, and a description that only the request
model rejects fails *after* the whole project has been uploaded.
"""

import click
import pytest
from click.testing import CliRunner

from robovast.client import cluster_cli, exec_cli


@pytest.fixture
def launch(monkeypatch, tmp_path):
    """Capture the kwargs of ``run_project_via_service`` instead of launching."""
    project = tmp_path / "myproj"
    project.mkdir()
    vast = project / "demo.vast"
    vast.write_text("version: 2\n")

    captured = {}

    def fake_run(client, config_path, **kwargs):  # noqa: ARG001
        captured.update(kwargs, config_path=config_path)
        return "camp-1"

    monkeypatch.setattr("robovast.service.project_push.run_project_via_service", fake_run)
    # Patch where the name is *bound*, not where it is defined: the cluster CLI
    # imports it at module level, so rebinding the source module would not be seen.
    monkeypatch.setattr(
        "robovast.client.cluster_cli.service_client",
        lambda *a, **k: _yield_client())
    monkeypatch.setattr("robovast.client.project_config.ProjectConfig.load",
                        classmethod(lambda cls, start_dir=None: None))
    captured["_vast"] = str(vast)
    return captured


def _yield_client():
    import contextlib

    @contextlib.contextmanager
    def _cm():
        yield object(), "test target"
    return _cm()


def _invoke(vast, *args):
    return CliRunner().invoke(
        cluster_cli.cluster, ['run', *args],
        obj={'vast_file': vast}, catch_exceptions=False)


def test_runs_defaults_to_the_vast_not_one(launch):
    result = _invoke(launch["_vast"])
    assert result.exit_code == 0, result.output
    # 0 means "use execution.runs"; any other stand-in for "unset" is a silent override.
    assert launch["runs"] == 0


def test_explicit_runs_is_forwarded(launch):
    _invoke(launch["_vast"], '--runs', '7')
    assert launch["runs"] == 7


def test_description_and_workspace_reach_the_launch(launch):
    _invoke(launch["_vast"], '--description', 'pilot: 5 reps', '--workspace', 'ws-name')
    assert launch["description"] == 'pilot: 5 reps'
    assert launch["workspace_name"] == 'ws-name'
    assert launch["on_exists"] is cluster_cli._confirm_overwrite


def test_an_over_long_description_is_refused_before_anything_is_pushed(launch):
    from robovast.service.interface import DESCRIPTION_MAX_LEN

    result = _invoke(launch["_vast"], '--description', 'x' * (DESCRIPTION_MAX_LEN + 1))
    assert result.exit_code != 0
    assert "shorten it" in result.output
    assert not launch.get("description"), "the project must not be pushed first"


def test_campaign_id_is_not_accepted(launch):
    # The service names the campaign and CreateCampaignRequest carries no id, so an
    # accepted --campaign-id could only be ignored. Refusing the option says so.
    result = CliRunner().invoke(
        exec_cli.execution, ['cluster', 'run', '--campaign-id', 'my-id'],
        obj={'vast_file': launch["_vast"]})
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_overwrite_prompt_proceeds_off_a_terminal(monkeypatch, capsys):
    # A scripted launch has nobody to ask; blocking would hang it. Proceed, but say so.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cluster_cli._confirm_overwrite("myproj", "ws-1") is True
    assert "not a terminal" in capsys.readouterr().out


def test_overwrite_prompt_defaults_to_yes_on_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    seen = {}

    def fake_confirm(question, default=None):
        seen.update(question=question, default=default)
        return default

    monkeypatch.setattr(click, "confirm", fake_confirm)
    assert cluster_cli._confirm_overwrite("myproj", "ws-1") is True
    assert seen["default"] is True          # Enter is enough
    assert "myproj" in seen["question"] and "ws-1" in seen["question"]
