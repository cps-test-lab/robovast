# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``exec_in_container`` / ``stop_container`` MCP tools.

The behavioural rules live in ``tests/service/test_container_exec.py``. What is guarded
here is what the *surface* promises: that the tool says plainly it produces no campaign
data (the one failure mode of offering it at all), that it exposes no way to reach a
running campaign's container, and that output trimming is the same code the log tools use.
"""

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import execution
from robovast.service.interface import ExecContainerState, ExecResult, ExecStopResult, ResourceUsage


class _FakeClient:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or ExecResult(exit_code=0, stdout="out", stderr="")

    def exec_in_container(self, request):
        self.calls.append(("exec", request))
        return self._result

    def stop_exec_container(self):
        self.calls.append(("stop",))
        return ExecStopResult(stopped=True, target="robovast-exec")

    def resource_usage(self):
        return ResourceUsage(
            backend="docker", cpu_capacity=8, cpu_used=1,
            memory_capacity_bytes=16, memory_used_bytes=2, parallel_runs=False,
            exec_container=ExecContainerState(kept=True, image="img:1",
                                              deadline_in_s=300))


@pytest.fixture
def service(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    return fake


# -- the framing, which is the point ----------------------------------------


def test_the_docstring_says_what_it_is_for_and_what_it_does_not_produce():
    """An agent that mistakes this for a way to run experiments is the real risk.

    Asserted rather than trusted because the tool surface has a hard token budget: the
    pressure to trim prose is real and constant, and this is the prose that must survive
    it. Trim the argument list instead.
    """
    doc = execution.exec_in_container.__doc__
    assert "Test a container and its setup" in doc
    assert "no campaign data" in doc.lower()
    assert "start_campaign" in doc, "must point at what actually runs the experiment"
    for absent in ("no provenance", "no repetitions"):
        assert absent in doc


def test_the_docstring_explains_that_a_scenarios_output_is_a_file_not_stdout():
    # Without this, case 2 looks broken: entrypoint.sh redirects its own stdout when
    # given no argv, so `stdout` comes back near-empty and the log is in the container.
    doc = execution.exec_in_container.__doc__
    assert "log_path" in doc
    assert "tail" in doc


def test_the_single_container_rule_is_stated():
    assert "At most one container" in execution.exec_in_container.__doc__


# -- no route to a running campaign's container -----------------------------


def test_the_surface_offers_no_way_to_reach_a_campaigns_container():
    """A campaign in flight is provenance-recorded compute; attaching would perturb it.

    ``campaign_id`` here names a *config source* (its read-only ``_config/``), never a
    container — so there must be no pod/job selector on this tool.

    ``container`` is **not** such a selector, which is worth stating because the word
    invites the assumption: it picks which of the campaign's declared containers a fresh
    throwaway one is started *from* (``scenario`` / ``simulation`` / ``sut``). Nothing
    here can reach a container a campaign is running.
    """
    import inspect
    params = inspect.signature(execution.exec_in_container).parameters
    assert "job_name" not in params
    assert "pod" not in params
    assert "campaign_id" in params
    assert "container" in params


# -- routing ----------------------------------------------------------------


def test_the_request_is_passed_through_verbatim(service):
    execution.exec_in_container(command="ros2 pkg list", workspace_id="ws1",
                               config_path="a.vast", config_name="c1",
                               keep_alive=True)
    name, req = service.calls[-1]
    assert name == "exec"
    assert req.command == "ros2 pkg list"
    assert (req.workspace_id, req.config_path, req.config_name) == ("ws1", "a.vast", "c1")
    assert req.keep_alive is True


def test_no_timeout_can_be_passed_from_here(service):
    """The limit is derived from what is being run, so no client may override it."""
    import inspect
    assert "timeout_s" not in inspect.signature(execution.exec_in_container).parameters
    execution.exec_in_container(command="ls", workspace_id="w")
    _name, req = service.calls[-1]
    assert not hasattr(req, "timeout_s")


def test_a_validation_failure_is_returned_as_an_error_dict(monkeypatch):
    class Refusing(_FakeClient):
        def exec_in_container(self, request):
            raise ValueError("no source named: pass workspace_id")

    monkeypatch.setattr(service_access, "service_client", lambda: Refusing())
    out = execution.exec_in_container(command="ls")
    assert "no source named" in out["error"]


def test_no_service_is_reported_not_worked_around(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    assert "error" in execution.exec_in_container(command="ls", workspace_id="w")
    assert "error" in execution.stop_container()


# -- output trimming is the log tools' own filter ---------------------------


def test_long_output_is_trimmed_by_tail_and_reports_the_total(monkeypatch):
    result = ExecResult(exit_code=0, stdout="\n".join(f"line{i}" for i in range(500)))
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _FakeClient(result=result))
    out = execution.exec_in_container(command="ls", workspace_id="w", tail=10)
    assert out["stdout"].splitlines()[-1] == "line499"
    assert len(out["stdout"].splitlines()) == 10
    assert out["stdout_truncated"] is True
    assert out["stdout_lines_total"] == 500


def test_log_path_is_absent_rather_than_empty_when_no_scenario_was_started(service):
    # An empty string would read as "the log is at ''".
    out = execution.exec_in_container(command="ls", workspace_id="w")
    assert "log_path" not in out


def test_a_started_scenario_reports_its_log_path(monkeypatch):
    result = ExecResult(exit_code=0, log_path="/tmp/robovast-exec/logs/system.log")
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _FakeClient(result=result))
    out = execution.exec_in_container(command="", workspace_id="w", config_name="c1")
    assert out["log_path"] == "/tmp/robovast-exec/logs/system.log"


# -- stop, and attributing the memory it frees -----------------------------


def test_stop_routes_and_reports(service):
    assert execution.stop_container() == {"stopped": True, "target": "robovast-exec"}


def test_resource_usage_names_a_held_container(service):
    """A caller told only "the lane is full" cannot discover its own container is why."""
    out = execution.get_resource_usage()
    assert out["exec_container"]["kept"] is True
    assert out["exec_container"]["image"] == "img:1"
