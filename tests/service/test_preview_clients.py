# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The CLI and the MCP tool wait for a preview by polling, and say what it waits on.

A preview that first builds the campaign's image takes as long as the build. Polled, the
service answers ``composing`` with the build in ``stage``: the CLI prints it while it waits,
and the MCP tool answers with it and the call to repeat once its bounded wait is over.
"""

import contextlib

import pytest
from click.testing import CliRunner

from robovast.client import cli as root_cli
from robovast.client.preview import PreviewFailed, await_preview
from robovast.service.interface import PreviewConfiguration, PreviewResponse

BUILDING = ("building the campaign's image for container 'simulation' (build b-simulation); "
            "the simulator is asked in it once it is built")
READY = PreviewResponse(state="ready", configs=1, runs_per_config=1, total_trials=1, jobs=1,
                        configurations=[PreviewConfiguration(name="depot")])


class _Service:
    """Answers each poll from a script, its last answer for ever after."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def preview_configurations(self, workspace_id, max_configs=0, path="", wait=True):
        self.asked.append(wait)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _building():
    return PreviewResponse(state="composing", stage=BUILDING)


def test_a_landed_answer_on_the_first_call_is_composed_again():
    """It was composed before this call was asked, and may predate an edit."""
    stale = READY.model_copy(update={"configs": 7})
    service = _Service(stale, _building(), READY)
    assert await_preview(service, "ws-1", poll_s=0) is READY
    assert len(service.asked) == 3


def test_each_stage_is_said_once_and_the_ready_preview_returned():
    service = _Service(_building(), _building(), PreviewResponse(state="composing"), READY)
    said = []
    answer = await_preview(service, "ws-1", on_stage=said.append, poll_s=0)
    assert answer is READY
    assert said == [BUILDING]
    assert set(service.asked) == {False}, "never a waited call"


def test_a_bounded_wait_returns_the_preview_as_it_stands():
    answer = await_preview(_Service(_building()), "ws-1", deadline_s=0, poll_s=0)
    assert answer.state == "composing" and answer.stage == BUILDING


def test_a_failed_preview_raises_its_reason():
    service = _Service(_building(), PreviewResponse(state="failed", error="did not build"))
    with pytest.raises(PreviewFailed, match="did not build"):
        await_preview(service, "ws-1", poll_s=0)


@pytest.fixture
def cli_service(monkeypatch):
    def _use(service):
        @contextlib.contextmanager
        def _client(*_a, **_k):
            yield service, "fake service"

        monkeypatch.setattr(root_cli, "service_client", _client)
        monkeypatch.setattr("robovast.service.project_push._resolve_workspace_id",
                            lambda _client, ref: ref)
    return _use


def test_the_cli_says_the_image_is_building_then_prints_the_preview(cli_service, monkeypatch):
    monkeypatch.setattr("robovast.client.preview.time.sleep", lambda _s: None)
    cli_service(_Service(_building(), READY))
    result = CliRunner().invoke(root_cli.workspace, ["preview", "ws-1"])
    assert result.exit_code == 0, result.output
    assert BUILDING in result.output
    assert "depot" in result.output
    assert "1 configuration(s)" in result.output


def test_a_search_preview_says_its_names_are_a_sample(cli_service, monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import authoring

    sample = READY.model_copy(update={"search": True})
    monkeypatch.setattr("robovast.client.preview.time.sleep", lambda _s: None)
    cli_service(_Service(sample))
    result = CliRunner().invoke(root_cli.workspace, ["preview", "ws-1"])
    assert result.exit_code == 0, result.output
    assert "--filter does not apply" in result.output

    monkeypatch.setattr(service_access, "service_client", lambda: _Service(sample))
    assert authoring.preview_configurations("/sources/ws-1/campaign.vast")["search"] is True


def test_the_cli_reports_a_failed_preview(cli_service, monkeypatch):
    monkeypatch.setattr("robovast.client.preview.time.sleep", lambda _s: None)
    cli_service(_Service(PreviewResponse(state="failed", error="did not build")))
    result = CliRunner().invoke(root_cli.workspace, ["preview", "ws-1"])
    assert result.exit_code != 0
    assert "did not build" in result.output


def test_the_mcp_tool_answers_with_what_it_waits_on_and_the_call_to_repeat(monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import authoring

    service = _Service(_building())
    monkeypatch.setattr(service_access, "service_client", lambda: service)
    monkeypatch.setattr(authoring, "_PREVIEW_WAIT_S", 0)
    monkeypatch.setattr("robovast.client.preview.time.sleep", lambda _s: None)

    reply = authoring.preview_configurations("/sources/ws-1/campaign.vast")

    assert reply["state"] == "composing"
    assert reply["stage"] == BUILDING
    assert "preview_configurations again" in reply["next_step"]


def test_the_mcp_tool_reports_a_failed_preview_as_its_error(monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import authoring

    failed = PreviewResponse(state="failed", error="did not build")
    monkeypatch.setattr(service_access, "service_client", lambda: _Service(failed))
    monkeypatch.setattr("robovast.client.preview.time.sleep", lambda _s: None)

    with pytest.raises(PreviewFailed, match="did not build"):
        authoring.preview_configurations("/sources/ws-1/campaign.vast")


def test_the_mcp_tool_returns_the_preview_once_it_is_ready(monkeypatch):
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins import authoring

    monkeypatch.setattr(service_access, "service_client", lambda: _Service(_building(), READY))
    monkeypatch.setattr("robovast.client.preview.time.sleep", lambda _s: None)

    reply = authoring.preview_configurations("/sources/ws-1/campaign.vast")

    assert reply["configs"] == 1
    assert [c["name"] for c in reply["configurations"]] == ["depot"]
