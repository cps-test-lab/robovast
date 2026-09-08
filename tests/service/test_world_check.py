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


import os
import shutil
import subprocess

import pytest

from robovast.service import world_query
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


# -- the runner: how a held container is given what a mount would provide ----


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
    assert "components:" in script, "the document's content has to reach the container"
    assert "mkdir -p" in script


def test_a_documents_paths_are_rewritten_the_way_argv_is(tmp_path):
    """A path inside the staged document needs the same rewrite the command gets.

    The override tree is exactly where a campaign names a file argv cannot carry -- a
    floorplan mesh, say -- so the paths that most need rewriting travel in the document
    rather than on argv. Rewriting only argv reported such a mesh as missing on every
    configuration whose ``sim:`` block named one, while the campaign mounted it and ran.
    """
    document = tmp_path / "sim.overrides.yaml"
    document.write_text(
        "plugins:\n  floorplan:\n    mesh: /config/environments/hexagon/hexagon.stl\n")
    exec_call = _Exec()
    runner = ExecSlotContainerRunner(exec_call, workspace_id="ws-1",
                                     config_path="a.vast")
    runner.expose(str(tmp_path), "/config")
    runner.expose(str(document), "/aux/sim.overrides.yaml")
    runner.run(["roqsim", "scenes", "describe", "w.yaml",
                "--override", "/aux/sim.overrides.yaml"])
    script = exec_call.requests[-1].command
    assert "/sources/ws-1/environments/hexagon/hexagon.stl" in script
    assert "/config/environments" not in script


def test_a_rewrite_does_not_reach_inside_a_longer_name(tmp_path):
    """``/config`` is a path, not a prefix: ``/configuration`` is a different directory."""
    document = tmp_path / "sim.overrides.yaml"
    document.write_text("plugins:\n  a:\n    path: /configuration/keep-me\n")
    exec_call = _Exec()
    runner = ExecSlotContainerRunner(exec_call, workspace_id="ws-1",
                                     config_path="a.vast")
    runner.expose(str(tmp_path), "/config")
    runner.expose(str(document), "/aux/sim.overrides.yaml")
    runner.run(["roqsim", "scenes", "describe", "w.yaml"])
    assert "/configuration/keep-me" in exec_call.requests[-1].command


def test_a_document_is_staged_where_this_container_may_actually_write(tmp_path):
    """The path a backend names on argv is an aux Pod's ``emptyDir``, and this is not that Pod.

    Writing the document there verbatim ran ``mkdir -p /aux`` as an unprivileged user, which
    failed before the simulator was asked anything -- and the campaign's own world was then
    reported as one that does not load. So the file is staged somewhere writable and argv is
    rewritten onto it, the same mechanism a directory already used.
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
    assert "/aux" not in script, (
        "neither the write nor argv may name a mount point this container does not have")
    staged = "/tmp/robovast-world-query/sim.overrides.yaml"
    assert f"cat > {staged}" in script, "the document has to be written where it may be written"
    assert f"--override {staged}" in script, (
        "argv has to name the staged path, or the simulator reads a file nothing wrote")


def test_a_document_is_written_as_data_not_as_shell():
    """A quoted heredoc delimiter. An override carrying ``$`` or a backtick would otherwise
    reach the simulator altered, which is worse than failing outright."""
    import inspect

    source = inspect.getsource(world_query.ExecSlotContainerRunner._script)
    assert "<<'" in source, "the heredoc delimiter must be quoted"


def test_the_staged_script_is_valid_shell(tmp_path, monkeypatch):
    """The script this builds has to be a script. Nothing above ever checked that.

    A heredoc ends only at a line holding *exactly* its delimiter. Joining the parts with
    ``&&`` left the terminator sharing its line with the command that followed, so bash
    never closed the document -- it swallowed the describe, the script still exited 0, and
    every campaign carrying overrides came back "was NOT checked".
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash to check the script against")
    monkeypatch.setattr(world_query, "_STAGE_DIR", str(tmp_path / "stage"))
    document = tmp_path / "sim.overrides.yaml"
    document.write_text("components:\n  robot:\n    pos: [1, 2]\n")
    exec_call = _Exec()
    runner = ExecSlotContainerRunner(exec_call, workspace_id="ws-1",
                                     config_path="a.vast")
    runner.expose(str(document), "/aux/sim.overrides.yaml")
    runner.run(["roqsim", "scenes", "describe", "w.yaml",
                "--override", "/aux/sim.overrides.yaml"])
    script = tmp_path / "staged.sh"
    script.write_text(exec_call.requests[-1].command)
    checked = subprocess.run([bash, "-n", str(script)], capture_output=True,
                             text=True, check=False)
    # stderr, not only the exit code: an unterminated heredoc is a *warning*, and bash
    # exits 0 on it. Asserting the returncode alone is how this shipped.
    assert checked.returncode == 0, checked.stderr
    assert checked.stderr == "", checked.stderr


def test_the_command_runs_after_the_document_is_staged(tmp_path, monkeypatch):
    """Staging the document and asking the question are one script; both must happen.

    Every substring assertion above passed while the command never ran at all -- it had
    been absorbed into the heredoc. Only running the script tells those two apart, so this
    runs it against a stub simulator and reads what the stub was actually given.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash to run the script in")
    stage = tmp_path / "stage"
    monkeypatch.setattr(world_query, "_STAGE_DIR", str(stage))
    document = tmp_path / "sim.overrides.yaml"
    # `$` and a backtick ride along: the quoted delimiter is what keeps an override tree
    # from being expanded on its way in, and the fix must not cost that.
    body = "components:\n  robot:\n    pos: [1, 2]\n  note: $HOME `id`\n"
    document.write_text(body)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "roqsim"
    stub.write_text('#!/bin/sh\necho "$@" > "$ROQSIM_STUB_ARGV"\n')
    stub.chmod(0o755)

    exec_call = _Exec()
    runner = ExecSlotContainerRunner(exec_call, workspace_id="ws-1",
                                     config_path="a.vast")
    runner.expose(str(document), "/aux/sim.overrides.yaml")
    runner.run(["roqsim", "scenes", "describe", "w.yaml",
                "--override", "/aux/sim.overrides.yaml"])

    argv_file = tmp_path / "argv"
    ran = subprocess.run(
        [bash, "-c", exec_call.requests[-1].command],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
             "ROQSIM_STUB_ARGV": str(argv_file)})
    assert ran.returncode == 0, ran.stderr

    staged = stage / "sim.overrides.yaml"
    assert staged.read_text() == body, (
        "the document has to reach the container verbatim, `$` and backticks included")
    assert argv_file.exists(), "the simulator was never asked: the heredoc ate the command"
    assert argv_file.read_text().split() == [
        "scenes", "describe", "w.yaml", "--override", str(staged)]


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


# -- the three answers a report may give, told apart without reading English ------


def test_an_unchecked_world_is_marked_unchecked_not_error(tmp_path, monkeypatch):
    """The severity, not the wording, is what a caller branches on. Until this field
    existed the only way to tell the two apart was to search the message for a phrase."""
    from robovast.common import config_generation
    from robovast.service.world_query import world_problems

    def _refuse(*a, **k):
        raise config_generation.WorldQueryUnavailable("no container runner is available")

    monkeypatch.setattr(config_generation, "describe_world_payload", _refuse)
    problems = world_problems(_Exec(), workspace_id="ws-1", config_path="a.vast",
                              vast_dir=str(tmp_path), parameters=_parameters())
    assert [p["severity"] for p in problems] == ["unchecked"]


def test_a_world_that_does_not_load_is_an_error(tmp_path, monkeypatch):
    from robovast.common import config_generation
    from robovast.service.world_query import world_problems

    monkeypatch.setattr(
        config_generation, "describe_world_payload",
        lambda *a, **k: ({"errors": {"build": "resource not found"}}, "roqsim:test"))
    problems = world_problems(_Exec(), workspace_id="ws-1", config_path="a.vast",
                              vast_dir=str(tmp_path), parameters=_parameters())
    assert [p["severity"] for p in problems] == ["error"]


def test_the_reason_a_query_could_not_run_names_what_would_settle_it(tmp_path, monkeypatch):
    """A reason with no remedy leaves a caller editing the .vast for a failure that was
    never about the file."""
    from robovast.common import config_generation
    from robovast.service.world_query import world_problems

    def _refuse(*a, **k):
        raise config_generation.WorldQueryUnavailable(
            "roqsim could not describe this world", next_step="check the lane")

    monkeypatch.setattr(config_generation, "describe_world_payload", _refuse)
    problems = world_problems(_Exec(), workspace_id="ws-1", config_path="a.vast",
                              vast_dir=str(tmp_path), parameters=_parameters())
    assert "Next: check the lane" in problems[0]["message"]


def _two_worlds(monkeypatch):
    """Make the campaign resolve to two distinct worlds.

    Patched rather than authored: a second block only appears once the backend resolves
    and validates the configuration's own ``sim:``, which needs the simulator package
    installed. What is under test is what ``world_problems`` does with two blocks.
    """
    monkeypatch.setattr(world_query, "_distinct_blocks",
                        lambda *a, **k: [(None, {"config": "world.yaml"}),
                                         ("other", {"config": "other-world.yaml"})])


def test_one_lane_failure_is_reported_once_not_once_per_world(tmp_path, monkeypatch):
    """A lane that cannot start a container fails every block for the same reason, and
    saying so once per block is a reply that scales with the campaign, not the problem."""
    from robovast.common import config_generation
    from robovast.service.world_query import world_problems

    def _refuse(*a, **k):
        raise config_generation.WorldQueryUnavailable("no container runner is available")

    _two_worlds(monkeypatch)
    monkeypatch.setattr(config_generation, "describe_world_payload", _refuse)
    problems = world_problems(_Exec(), workspace_id="ws-1", config_path="a.vast",
                              vast_dir=str(tmp_path), parameters=_parameters())
    assert len(problems) == 1, "one cause, one problem"
    assert problems[0]["config"] is None, "it is about the campaign, not one cell"


def test_two_worlds_failing_differently_stay_two_problems(tmp_path, monkeypatch):
    """The collapse must never hide a difference between configurations — which is the
    whole reason the blocks are described one by one."""
    from robovast.common import config_generation
    from robovast.service.world_query import world_problems

    def _refuse(_execution, block, *a, **k):
        raise config_generation.WorldQueryUnavailable(
            f"cannot describe {block.get('config')}")

    _two_worlds(monkeypatch)
    monkeypatch.setattr(config_generation, "describe_world_payload", _refuse)
    problems = world_problems(_Exec(), workspace_id="ws-1", config_path="a.vast",
                              vast_dir=str(tmp_path), parameters=_parameters())
    assert len(problems) == 2
    assert {p["config"] for p in problems} == {None, "other"}


# -- what the report says about a check that did not run -------------------------


class _Registry:
    def require(self, workspace_id):
        return {"workspace_id": workspace_id}


class _Store:
    registry = _Registry()


class _Transport:
    """Enough of ``LocalTransport`` for ``_with_world_check``, which is what is under test."""

    store = _Store()

    def exec_in_container(self, _request):
        raise AssertionError("the world query is patched out in these tests")


def _checked(monkeypatch, tmp_path, problems=None, crash=None):
    """``_with_world_check`` over a clean cheap-check result.

    The query and the file load are both stood in for: what is under test is the verdict
    this builds from an answer, not how the answer or the file was obtained.
    """
    from robovast.common import common
    from robovast.service.local_transport import LocalTransport

    def _answer(*a, **k):
        if crash is not None:
            raise crash
        return list(problems or [])

    monkeypatch.setattr(world_query, "world_problems", _answer)
    monkeypatch.setattr(common, "load_config", lambda *a, **k: _parameters())

    class _Project:
        config_path = str(tmp_path / "a.vast")

    return LocalTransport._with_world_check(  # noqa: SLF001 - the unit under test
        _Transport(), "ws-1", "", _Project(), {"valid": True, "problems": []})


def test_a_checked_world_says_so_in_the_field_not_only_by_silence(tmp_path, monkeypatch):
    result = _checked(monkeypatch, tmp_path)
    assert result["valid"] is True
    assert result["world_checked"] is True


def test_an_unchecked_world_is_not_a_valid_campaign(tmp_path, monkeypatch):
    """The defect this closes: an advisory said the world went unchecked while ``valid``
    stayed true, so a caller that branches on the boolean — which is what a boolean is for
    — ran a sweep whose most expensive failure had never been looked for."""
    result = _checked(monkeypatch, tmp_path, problems=[
        {"stage": "world", "config": None, "field": "f", "severity": "unchecked",
         "message": "this campaign's world was NOT checked: no container runner"}])
    assert result["valid"] is False
    assert result["world_checked"] is False


def test_a_check_that_crashed_is_reported_rather_than_logged_and_dropped(
        tmp_path, monkeypatch):
    """A caller cannot read this service's log, so a swallowed failure returned a reply
    that had checked nothing and said so nowhere."""
    result = _checked(monkeypatch, tmp_path, crash=RuntimeError("the store is gone"))
    assert result["valid"] is False
    assert result["world_checked"] is False
    assert [p["severity"] for p in result["problems"]] == ["unchecked"]
    assert "the store is gone" in result["problems"][0]["message"]


def test_an_advisory_about_a_checked_world_still_passes(tmp_path, monkeypatch):
    """``advice`` is a checked fact worth saying, not a reason to refuse a campaign. Only
    ``error`` and ``unchecked`` may take a pass away."""
    result = _checked(monkeypatch, tmp_path, problems=[
        {"stage": "world", "config": None, "field": "f", "severity": "advice",
         "message": "this world has no lighting"}])
    assert result["valid"] is True
    assert result["world_checked"] is True
