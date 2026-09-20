# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What the scenario probe actually catches, by running it.

`test_scenario_check.py` covers how a verdict is turned into a problem. This covers the script
that produces the verdict, because the gap was in the script: it built the internal model and
stopped, and binding an invocation to the action it names happens after that. So a call passing
an argument the action does not take -- the commonest way a scenario is wrong -- parsed clean
here and killed every trial of the campaign, once each, after the pull and the schedule.

Run in-process rather than in an image: the script is plain Python against `scenario_execution`,
and what is under test is which failures it reaches, not the container it reaches them in.
"""

import json
import subprocess
import sys
import textwrap

import pytest

from robovast.service.scenario_query import _PROBE

pytest.importorskip("scenario_execution", reason="the probe parses with scenario_execution")
pytest.importorskip("py_trees")


def _verdict(tmp_path, scenario: str) -> dict:
    path = tmp_path / "scenario.osc"
    path.write_text(textwrap.dedent(scenario), encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", _PROBE.format(path=repr(str(path)))],
                         capture_output=True, text=True, check=False)
    lines = [ln for ln in out.stdout.splitlines() if ln.strip().startswith("{")]
    assert lines, f"the probe printed no verdict: {out.stdout!r} {out.stderr!r}"
    return json.loads(lines[-1])


def test_a_clean_scenario_passes(tmp_path):
    assert _verdict(tmp_path, """
        import osc.helpers

        scenario test:
            do serial:
                log("hello")
        """) == {"ok": True}


def test_an_argument_the_action_does_not_take_is_caught(tmp_path):
    """The failure that reached a campaign. The model builds; only resolution binds this call
    to `log` and finds that it has no `nonsense`."""
    verdict = _verdict(tmp_path, """
        import osc.helpers

        scenario test:
            do serial:
                log(nonsense: 'x')
        """)
    assert "error" in verdict, verdict
    assert "nonsense" in verdict["error"]


def test_an_unresolvable_import_is_still_caught(tmp_path):
    verdict = _verdict(tmp_path, """
        import osc.no_such_library

        scenario test:
            do serial:
                log("hello")
        """)
    assert "error" in verdict, verdict
    assert "no_such_library" in verdict["error"]


def test_a_parameter_with_no_value_is_not_the_scenarios_defect(tmp_path):
    """The configuration supplies those and this check has none to give, so an unbound
    parameter must not be reported as a broken scenario -- every campaign has them."""
    assert _verdict(tmp_path, """
        import osc.helpers

        scenario test:
            map_file: string
            do serial:
                log(map_file)
        """) == {"ok": True}


def test_a_bad_call_is_still_caught_in_a_scenario_that_has_parameters(tmp_path):
    """The tolerance above must not swallow the real thing: arguments are bound before a
    parameter's value is needed, so the defect is still reached."""
    verdict = _verdict(tmp_path, """
        import osc.helpers

        scenario test:
            map_file: string
            do serial:
                log(nonsense: map_file)
        """)
    assert "error" in verdict, verdict
    assert "nonsense" in verdict["error"]
