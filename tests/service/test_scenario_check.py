# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The scenario check: does the scenario parse in the image that would run it?

The sibling of the world check, guarding a failure of the same shape and a worse ending.
An ``import osc.<library>`` resolves against what is installed where the scenario runs, so
a scenario that parses everywhere else can die at its first line in every trial — and the
campaign still reports finished, because every run started and none of them said anything
a status reads as a fault.

Three rules the tests below pin down:

- the verdict is DATA, not an exit code. "The scenario is broken" and "I could not ask"
  must be distinguishable, and a shell that exits non-zero for both makes them one answer;
- a check that could not run is ``unchecked``, never a pass;
- a clean scenario adds nothing to the reply.
"""

import json

import pytest

from robovast.service.scenario_query import scenario_problems


class _Result:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _Exec:
    """Records the ExecRequest it was handed and replies with a canned result."""

    def __init__(self, result=None, raises=None):
        self.requests = []
        self.result = result or _Result(stdout=json.dumps({"ok": True}))
        self.raises = raises

    def __call__(self, request):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return self.result


def _check(exec_call):
    return scenario_problems(exec_call, workspace_id="ws-1", config_path="demo.vast",
                             scenario_path="sim/scenario.osc")


def test_the_parse_runs_in_the_scenario_container_on_the_query_pool():
    """Where it runs is the whole point: the answer differs per image.

    The scenario container, because that is where the scenario runs — and the one
    container every campaign has, including one with no simulator at all. The query pool,
    because this is read-only and repeated all through an authoring loop.
    """
    exec_call = _Exec()
    assert _check(exec_call) == []

    request = exec_call.requests[-1]
    assert request.container == "scenario"
    assert request.query is True, "a validation query must not disturb a held container"
    assert "/sources/ws-1/sim/scenario.osc" in request.command, \
        "the exec lane mounts the workspace, so the path is spelled from there"


def test_an_unresolved_import_is_the_campaigns_defect_and_fails_validation():
    """The failure the check exists for, reported with the parser's own words.

    The message names the file, the line, the column and the library by the name the
    author wrote. Rephrasing it would lose the position, which is what makes it something
    to act on rather than something to investigate.
    """
    parser_says = ('Error creating internal model: (line: 2, column: 7) -> osc.roqsim: '
                   'No import library "roqsim" found.')
    exec_call = _Exec(_Result(stdout=json.dumps({"error": parser_says})))

    problems = _check(exec_call)

    assert len(problems) == 1
    assert problems[0]["severity"] == "error", "this is a defect in the campaign"
    assert problems[0]["stage"] == "scenario"
    assert parser_says in problems[0]["message"]


def test_an_image_without_a_parser_is_unchecked_rather_than_broken():
    """"I could not ask" is not "your scenario is wrong", and not a pass either."""
    exec_call = _Exec(_Result(stdout=json.dumps(
        {"unchecked": "this image has no scenario_execution to parse with (ImportError)"})))

    problems = _check(exec_call)

    assert len(problems) == 1
    assert problems[0]["severity"] == "unchecked"
    assert "NOT parsed" in problems[0]["message"]


def test_the_verdict_is_read_from_the_last_json_line_not_the_first():
    """An image talks on the way up, and some of what it says looks like data.

    A ROS setup, a deprecation notice, a plugin registering itself — the probe's line is
    the last JSON object in the output, and reading the first would take a startup
    message's word for the campaign's verdict.
    """
    noise = ('{"level": "info", "msg": "ros2 environment sourced"}\n'
             'not json at all\n')
    exec_call = _Exec(_Result(stdout=noise + json.dumps({"error": "line 4: boom"})))

    problems = _check(exec_call)

    assert len(problems) == 1 and "line 4: boom" in problems[0]["message"]


def test_no_verdict_at_all_is_unchecked_and_carries_what_was_said():
    """A container that died before the probe ran has still told us something.

    Dropping its output would leave the caller with "the check did not run" and no way to
    find out why — which on a missing image is exactly the sentence that names the fix.
    """
    exec_call = _Exec(_Result(exit_code=127, stderr="python3: not found"))

    problems = _check(exec_call)

    assert problems[0]["severity"] == "unchecked"
    assert "python3: not found" in problems[0]["message"]
    assert "build_experiment_image" in problems[0]["message"]


def test_the_check_crashing_is_reported_as_the_services_defect():
    """Not logged and dropped: a caller cannot see this service's log.

    And not blamed on the ``.vast`` either — the message says so in as many words, or the
    next hour goes into a file that was never the problem.
    """
    exec_call = _Exec(raises=RuntimeError("no lane"))

    problems = _check(exec_call)

    assert problems[0]["severity"] == "unchecked"
    assert "defect in the service" in problems[0]["message"]
    assert "no lane" in problems[0]["message"]


def test_the_probe_is_valid_python_that_reports_rather_than_raises():
    """The script is written here and run by the image's interpreter, so nothing type-checks
    it on the way. Compiling it is the cheapest guard against a probe that dies on a syntax
    error in every image and reports every campaign as unchecked."""
    from robovast.service.scenario_query import _PROBE

    compile(_PROBE.format(path=repr("/sources/ws-1/scenario.osc")), "<probe>", "exec")


def test_what_the_image_said_travels_with_a_parse_failure():
    """The JSON message is only as good as the exception behind it.

    A parser that logged its real detail before raising put that detail in the container's own
    output and nowhere else, and the caller cannot see this service's log. So the output rides
    along with the verdict.
    """
    noisy = ("[INFO] [entrypoint]: sourced /opt/ros\n"
             "ERROR reading scenario.osc: unexpected indent at line 12\n"
             + json.dumps({"error": "ValueError: parse failed"}))
    exec_call = _Exec(_Result(stdout=noisy))

    message = _check(exec_call)[0]["message"]

    assert "ValueError: parse failed" in message
    assert "unexpected indent at line 12" in message


def test_the_verdict_line_is_not_repeated_in_what_the_image_said():
    """Reporting the payload and then the raw line that carried it says the same thing twice."""
    exec_call = _Exec(_Result(stdout=json.dumps({"error": "boom"})))

    message = _check(exec_call)[0]["message"]

    assert message.count("boom") == 1


def test_an_image_that_cannot_parse_carries_its_own_output_too():
    """The unchecked side of the same rule: an import that failed inside a package leaves its
    traceback here, and it is the only place that says which package."""
    noisy = ("Traceback (most recent call last):\n"
             "  File \"<string>\", line 9\n"
             "ModuleNotFoundError: No module named 'antlr4'\n"
             + json.dumps({"unchecked": "this image has no scenario_execution to parse with"}))
    exec_call = _Exec(_Result(stdout=noisy))

    message = _check(exec_call)[0]["message"]

    assert "no scenario_execution to parse with" in message
    assert "antlr4" in message


def test_what_the_image_said_is_bounded():
    """A container that printed a megabyte must not push a megabyte into a validation reply."""
    exec_call = _Exec(_Result(stdout="noise\n" * 5000 + json.dumps({"error": "boom"})))

    message = _check(exec_call)[0]["message"]

    assert len(message) < 1500
    assert "boom" in message


def test_a_bare_exception_still_names_something():
    """``str(exc)`` is empty for an exception raised with no message.

    A refusal that names nothing is one nobody can act on, so the probe reports the type too.
    Run here against a stub parser, because the point is the probe's own formatting rather than
    anything scenario_execution does.
    """
    import io
    import sys
    import types
    from contextlib import redirect_stdout

    from robovast.service.scenario_query import _PROBE

    def _install(monkey):
        pkg = types.ModuleType("scenario_execution")
        model = types.ModuleType("scenario_execution.model")
        parser_mod = types.ModuleType("scenario_execution.model.osc2_parser")
        utils = types.ModuleType("scenario_execution.utils")
        logging_mod = types.ModuleType("scenario_execution.utils.logging")

        class _Parser:
            def __init__(self, _logger):
                pass

            def parse_file(self, *_a, **_k):
                raise ValueError()  # no message at all

        parser_mod.OpenScenario2Parser = _Parser
        logging_mod.Logger = lambda *_a, **_k: None
        for name, mod in (("scenario_execution", pkg),
                          ("scenario_execution.model", model),
                          ("scenario_execution.model.osc2_parser", parser_mod),
                          ("scenario_execution.utils", utils),
                          ("scenario_execution.utils.logging", logging_mod)):
            monkey[name] = mod

    saved = {k: v for k, v in sys.modules.items() if k.startswith("scenario_execution")}
    _install(sys.modules)
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), pytest.raises(SystemExit):
            exec(  # noqa: S102 - the probe is this repository's own source  # pylint: disable=exec-used
                compile(_PROBE.format(path=repr("/x.osc")), "<probe>", "exec"), {})
    finally:
        for key in [k for k in sys.modules if k.startswith("scenario_execution")]:
            del sys.modules[key]
        sys.modules.update(saved)

    payload = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert payload["error"] == "ValueError", "the type is all there is to report"
