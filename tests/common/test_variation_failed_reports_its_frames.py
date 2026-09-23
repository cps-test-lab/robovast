# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A plugin whose own code breaks reports the file and line it broke at.

Every surface -- the CLI, the MCP tools, the service's HTTP details, a campaign's recorded
error -- reduces the exception composition raises to its message. So the frames are
rendered into that message at the one site that catches the plugin, and the exception opts
out of a second rendering. A refusal the plugin means stays a one-line message.
"""

import textwrap

import pytest

from robovast.client.status import failure_detail
from robovast.common.config_generation import generate_scenario_variations
from robovast.common.config_validation import validate_project_file
from robovast.common.variation.base_variation import VariationFailed

_SCENARIO = """\
import osc.robotics

scenario nav:
    speed: length = 1.0m
    do serial:
        wait elapsed(1s)
"""


def _project(tmp_path, plugin_source, class_name="Broken"):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    (tmp_path / "broken.py").write_text(textwrap.dedent(plugin_source))
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 5
        metadata: {{name: broken-plugin-test}}
        configuration:
        - name: cell0
          variations:
          - broken.py:{class_name}: {{}}
        execution:
          containers:
            scenario: {{image: 'family:robovast'}}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


_RAISES_DIRECTLY = """\
    from robovast.common.variation.base_variation import Variation

    class Broken(Variation):
        def variation(self, in_configs):
            raise KeyError("no such slot")
"""

_BREAKS_IN_THE_LIBRARY = """\
    import json

    from robovast.common.variation.base_variation import Variation

    class Broken(Variation):
        def variation(self, in_configs):
            return json.loads("{")
"""

_REFUSES = """\
    from robovast.common.variation.base_variation import Variation, VariationInfeasibleError

    class Broken(Variation):
        def variation(self, in_configs):
            raise VariationInfeasibleError("no arrangement realizes this draw")
"""


def test_the_message_names_the_plugin_the_file_the_line_and_the_source(tmp_path):
    vast = _project(tmp_path, _RAISES_DIRECTLY)
    with pytest.raises(VariationFailed) as excinfo:
        generate_scenario_variations(str(vast), use_cache=False)
    message = str(excinfo.value)
    assert message.startswith("campaign.vast:6: config 'cell0': Variation failed. Broken: 'no such slot'")
    assert 'broken.py", line 5, in variation' in message
    assert 'raise KeyError("no such slot")' in message


def test_a_failure_inside_library_code_still_names_the_plugin_line(tmp_path):
    """The frame a reader can act on is the plugin's own, whichever module the exception
    surfaced in; the tail holds the whole chain down from it."""
    vast = _project(tmp_path, _BREAKS_IN_THE_LIBRARY)
    with pytest.raises(VariationFailed) as excinfo:
        generate_scenario_variations(str(vast), use_cache=False)
    message = str(excinfo.value)
    assert 'broken.py", line 7, in variation' in message
    assert 'return json.loads("{")' in message


def test_a_refusal_stays_one_line(tmp_path):
    """A draw the plugin declares unrealizable is what it says, not a crash: no frames."""
    from robovast.common.variation.base_variation import VariationInfeasibleError
    vast = _project(tmp_path, _REFUSES)
    with pytest.raises(VariationInfeasibleError) as excinfo:
        generate_scenario_variations(str(vast), use_cache=False)
    assert 'File "' not in str(excinfo.value)


def test_the_frames_are_carried_once(tmp_path):
    """A surface that records failures through ``failure_detail`` gets the message as it
    is: the exception carries its frames in the message and opts out of a second tail."""
    vast = _project(tmp_path, _RAISES_DIRECTLY)
    with pytest.raises(VariationFailed) as excinfo:
        generate_scenario_variations(str(vast), use_cache=False)
    exc = excinfo.value
    assert exc.include_traceback is False
    assert failure_detail(exc) == str(exc)
    assert str(exc).count('broken.py", line 5, in variation') == 1


def test_a_validation_report_carries_the_frames_through(tmp_path):
    """The check a project runs before spending compute passes the text on unchanged, so
    the MCP's ``validate_project`` and the web UI show the line too."""
    vast = _project(tmp_path, _RAISES_DIRECTLY)
    report = validate_project_file(str(vast))
    assert report["valid"] is False
    generation = [p for p in report["problems"] if p["stage"] == "generation"]
    assert len(generation) == 1
    assert 'broken.py", line 5, in variation' in generation[0]["message"]
