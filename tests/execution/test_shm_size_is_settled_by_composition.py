# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``/dev/shm`` sizing, composed from a real ``.vast``.

Composed rather than hand-fed: the Job manifest tests build their ``execution`` dict
directly, so only a test that starts from a file covers what composition hands the backend.
"""

import textwrap

import pytest

from robovast.common.config import DEFAULT_SHM_SIZE
from robovast.common.config_generation import generate_scenario_variations

_SCENARIO = """\
import osc.robotics

scenario nav:
    do serial:
        wait elapsed(1s)
"""


def _composed_execution(tmp_path, declared=None):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    line = f"  shm_size: {declared}\n" if declared else ""
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 5
        metadata: {name: shm}
        configuration:
        - name: base
        execution:
          mode: ros2
          containers:
            sut: {image: sut:latest}
            scenario: {image: scen:latest}
          runs: 1
          scenario_file: scenario.osc
        """) + line)
    data = generate_scenario_variations(
        str(vast), progress_update_callback=lambda m: None,
        output_dir=str(tmp_path / "gen"), use_cache=False)
    return data["execution"]


@pytest.mark.parametrize("declared,expected", [
    (None, DEFAULT_SHM_SIZE),
    ("2Gi", "2Gi"),
])
def test_composition_settles_the_size(tmp_path, declared, expected):
    """Unset means the default; declared means what was declared. There is no third state.

    The default is settled in composition, not in the backend that renders the pod.
    """
    assert _composed_execution(tmp_path, declared)["shm_size"] == expected
