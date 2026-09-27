# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A verb that cannot resolve what it was asked to act on refuses in one line.

No campaign to act on, several to choose from, a workspace name that matches nothing:
each is the caller's next move, not a defect, and a report with a type and frames under
it sends the reader to the wrong place."""

import contextlib
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from robovast.client import campaign_cli, cli as root_cli
from robovast.service.project_push import NoSuchWorkspace, _resolve_workspace_id


def _fake_service(monkeypatch, module, client):
    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield client, "fake service"
    monkeypatch.setattr(module, "service_client", _client)


def _campaigns(*phases):
    listed = [SimpleNamespace(campaign_id=f"c{i}", phase=phase)
              for i, phase in enumerate(phases, 1)]
    return SimpleNamespace(list_campaigns=lambda _req: SimpleNamespace(campaigns=listed))


def test_no_running_campaign_is_a_refusal(monkeypatch):
    _fake_service(monkeypatch, campaign_cli, _campaigns("finished"))
    result = CliRunner().invoke(campaign_cli.campaign, ["status"])
    assert result.exit_code == 1
    assert result.output.strip().endswith("Error: no campaign is running; pass CAMPAIGN.")
    assert "ValueError" not in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("verb", [
    ["status"], ["postprocess"], ["log"], ["log", "--json"], ["stop-job", "job-1"],
    ["tap", "job-1"], ["priority", "2"], ["pause"], ["resume"]])
def test_every_verb_that_needs_a_campaign_refuses_alike_without_one(monkeypatch, verb):
    """A verb that acts on one campaign and finds none has done nothing; exiting 0 would
    read as done."""
    _fake_service(monkeypatch, campaign_cli, _campaigns("finished"))
    result = CliRunner().invoke(campaign_cli.campaign, verb)
    assert result.exit_code == 1, result.output
    assert "Error: no campaign is running; pass CAMPAIGN." in result.stderr
    assert "Traceback" not in result.output
    if "--json" in verb:
        assert result.stdout == "", "stdout of a --json verb carries a document or nothing"


def test_stop_without_a_running_campaign_is_a_no_op(monkeypatch):
    """Nothing running is what a stop asks for, so it is already so."""
    _fake_service(monkeypatch, campaign_cli, _campaigns("finished"))
    result = CliRunner().invoke(campaign_cli.campaign, ["stop"])
    assert result.exit_code == 0, result.output
    assert "No running campaign found." in result.output


def test_several_running_campaigns_are_a_refusal_naming_them(monkeypatch):
    _fake_service(monkeypatch, campaign_cli, _campaigns("running", "running"))
    result = CliRunner().invoke(campaign_cli.campaign, ["stop"])
    assert result.exit_code == 1
    assert "2 campaigns are running (c1, c2); pass CAMPAIGN to choose one." in result.output
    assert "ValueError" not in result.output
    assert "Traceback" not in result.output


def test_a_workspace_name_that_matches_nothing_is_a_refusal():
    client = SimpleNamespace(list_workspaces=lambda: SimpleNamespace(workspaces=[]))
    with pytest.raises(NoSuchWorkspace, match="no workspace named 'nosuch'") as raised:
        _resolve_workspace_id(client, "nosuch")
    assert isinstance(raised.value, ValueError)
    assert raised.value.include_traceback is False


def test_the_refusal_reaches_the_terminal_as_one_line(monkeypatch, tmp_path):
    client = SimpleNamespace(list_workspaces=lambda: SimpleNamespace(workspaces=[]))
    _fake_service(monkeypatch, root_cli, client)
    result = CliRunner().invoke(root_cli.cli, ["workspace", "update", "nosuch", str(tmp_path)])
    assert result.exit_code == 1
    assert "Error: no workspace named 'nosuch'" in result.output
    assert "ValueError" not in result.output
    assert "Traceback" not in result.output
