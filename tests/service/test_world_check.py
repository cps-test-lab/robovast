# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The world check: does the campaign's world load, and does its model compile?

The failure this guards is per-trial and expensive — a world that does not compile fails
every run of the sweep, after the image pull and the pod schedule — and until this existed
``validate_project`` reported such a campaign as ``valid: true``.

Two rules the tests below pin down, because both are easy to lose:

- a check that could not RUN is an advisory, never a pass. Silence must not stand for
  "the world is fine";
- a clean world adds nothing to the reply. A tool that says "I checked, and it was fine"
  on every call is a line callers learn to skip.
"""

import json

import pytest

from robovast.service.world_query import ExecSlotContainerRunner


class _Result:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _Exec:
    """Records the ExecRequest it was handed and replies with a canned result."""

    def __init__(self, result=None):
        self.requests = []
        self.result = result or _Result()

    def __call__(self, request):
        self.requests.append(request)
        return self.result


# -- the runner: how a held container is given what a mount used to give it ----


def test_a_query_never_runs_in_the_callers_container():
    exec_call = _Exec()
    runner = ExecSlotContainerRunner(exec_call, workspace_id="ws-1",
                                     config_path="a.vast")
    runner.run(["roqsim", "scenes", "describe", "/config/world.yaml"])
    request = exec_call.requests[-1]
    assert request.query is True, "a world query must use the pool, not the user's slot"
    assert request.container == "simulation", "roqsim lives in the simulator's image"


def test_the_project_is_addressed_where_this_lane_actually_mounts_it(tmp_path):
    """``expose`` of a directory is a path rewrite, not a mount.

    A backend writes its command against ``CONFIG_MOUNT`` because that is where a *campaign*
    mounts the project. The exec lane already carries the same tree, at
    ``/sources/<workspace_id>`` — so the file is there, spelled differently, and rewriting
    is what lets one command address it in both places.
    """
    exec_call = _Exec()
    runner = ExecSlotContainerRunner(exec_call, workspace_id="ws-1",
                                     config_path="a.vast")
    runner.expose(str(tmp_path), "/config")
    runner.run(["roqsim", "scenes", "describe", "/config/worlds/depot.yaml"])
    assert "/sources/ws-1/worlds/depot.yaml" in exec_call.requests[-1].command
    assert "/config/worlds" not in exec_call.requests[-1].command


def test_an_override_document_travels_with_the_command(tmp_path):
    """A held container cannot gain a mount, so the document is written in ahead of it.

    It cannot simply be dropped: roqsim describes a *different world* without the campaign's
    overrides applied, and a caller comparing entity names against that answer reads a
    working campaign as a broken one.
    """
    document = tmp_path / "sim.overrides.yaml"
    document.write_text("components:\n  robot:\n    pos: [1, 2]\n")
    exec_call = _Exec()
    runner = ExecSlotContainerRunner(exec_call, workspace_id="ws-1",
                                     config_path="a.vast")
    runner.expose(str(document), "/aux/sim.overrides.yaml")
    runner.run(["roqsim", "scenes", "describe", "w.yaml",
                "--override", "/aux/sim.overrides.yaml"])
    script = exec_call.requests[-1].command
    assert "/aux/sim.overrides.yaml" in script
    assert "components:" in script, "the document's content has to reach the container"
    assert "mkdir -p" in script


def test_a_document_is_written_as_data_not_as_shell():
    """A quoted heredoc delimiter. An override carrying ``$`` or a backtick would otherwise
    reach the simulator altered, which is worse than failing outright."""
    import inspect

    from robovast.service import world_query
    source = inspect.getsource(world_query.ExecSlotContainerRunner._script)
    assert "<<'" in source, "the heredoc delimiter must be quoted"


def test_output_reaches_the_caller_even_when_the_command_failed():
    """``describe_world_payload`` recovers a PARTIAL answer from a failed run — a world
    that will not build can still say which components it has. Swallowing the output on
    failure would throw that half away."""
    exec_call = _Exec(_Result(exit_code=1, stdout='{"components": []}',
                              stderr="cannot build world"))
    runner = ExecSlotContainerRunner(exec_call, workspace_id="ws-1",
                                     config_path="a.vast")
    lines = []
    with pytest.raises(Exception):
        runner.run(["roqsim"], lines.append)
    assert any("components" in line for line in lines)
    assert any("cannot build" in line for line in lines)


# -- the check: what a caller is told ----------------------------------------


def _parameters(**execution):
    base = {"backend": "roqsim", "config": "world.yaml"}
    base.update(execution)
    return {"execution": {"mode": "ros2", "containers": {"simulation": base}}}


def test_a_campaign_with_no_simulator_is_not_asked_about_a_world(tmp_path):
    from robovast.service.world_query import world_problems
    called = _Exec()
    problems = world_problems(called, workspace_id="ws-1", config_path="a.vast",
                              vast_dir=str(tmp_path),
                              parameters={"execution": {"containers": {}}})
    assert problems == []
    assert not called.requests, "nothing to describe means no container"


def test_a_world_that_does_not_compile_is_reported_with_the_simulators_own_message(
        tmp_path, monkeypatch):
    """``errors.build`` carried verbatim. Paraphrasing it would drop the one thing that
    says *which* mesh or field is wrong."""
    from robovast.common import config_generation
    from robovast.service.world_query import world_problems

    monkeypatch.setattr(
        config_generation, "describe_world_payload",
        lambda *a, **k: ({"errors": {"build": "resource not found: 'meshes/shelf.obj'"}},
                         "roqsim:test"))
    problems = world_problems(_Exec(), workspace_id="ws-1", config_path="a.vast",
                              vast_dir=str(tmp_path), parameters=_parameters())
    assert len(problems) == 1
    assert problems[0]["stage"] == "world"
    assert "meshes/shelf.obj" in problems[0]["message"]
    assert "does not compile" in problems[0]["message"]


def test_a_world_that_could_not_be_asked_is_an_advisory_not_a_pass(tmp_path, monkeypatch):
    """The rule that keeps this honest: "I could not check" and "it is fine" are different
    answers, and only one of them may be silent."""
    from robovast.common import config_generation
    from robovast.service.world_query import world_problems

    def _refuse(*a, **k):
        raise config_generation.WorldQueryUnavailable(
            "this campaign's world is described by its own built image")

    monkeypatch.setattr(config_generation, "describe_world_payload", _refuse)
    problems = world_problems(_Exec(), workspace_id="ws-1", config_path="a.vast",
                              vast_dir=str(tmp_path), parameters=_parameters())
    assert len(problems) == 1
    assert "was NOT checked" in problems[0]["message"]


def test_a_clean_world_says_nothing_at_all(tmp_path, monkeypatch):
    from robovast.common import config_generation
    from robovast.service.world_query import world_problems

    monkeypatch.setattr(config_generation, "describe_world_payload",
                        lambda *a, **k: ({"components": [], "errors": None}, "roqsim:test"))
    assert world_problems(_Exec(), workspace_id="ws-1", config_path="a.vast",
                          vast_dir=str(tmp_path), parameters=_parameters()) == []
