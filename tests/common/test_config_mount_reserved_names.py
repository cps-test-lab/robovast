# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What a campaign may not stage at the config mount.

A configuration's file is staged where the campaign's copy would have been, and the run's
own furniture lives at that same mount: the entrypoint the container executes, the scenario
it runs, the parameter documents it reads. A deploy path equal to one of those would replace
it, so composition refuses it -- both lanes would otherwise discover it after the image
pull, one as a refused mount and the other as a pod that dies in its entrypoint.
"""

import textwrap

import pytest

from robovast.common.config_generation import generate_scenario_variations

_SCENARIO = """\
import osc.robotics

scenario nav:
    do serial:
        wait elapsed(1s)
"""


def _compose(tmp_path, source_rel, scenario_rel="scenario.osc"):
    scenario = tmp_path / scenario_rel
    scenario.parent.mkdir(parents=True, exist_ok=True)
    scenario.write_text(_SCENARIO)
    parent = tmp_path / source_rel
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("a: 1\n")
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 4
        metadata: {{name: reserved}}
        configuration:
        - name: only
        execution:
          containers:
            scenario: {{image: scen:latest}}
            sut:
              image: sut:latest
              config_files:
                cfg: {{file: {source_rel}, format: yaml}}
          runs: 1
          scenario_file: {scenario_rel}
        """))
    return generate_scenario_variations(
        variation_file=str(vast), output_dir=str(tmp_path / "out"),
        use_cache=False, isolate_plugins=False)


@pytest.mark.parametrize("reserved", [
    "entrypoint.sh",            # what the container executes
    "scenario.config",          # what the entrypoint reads parameters from by default
    "sim.overrides.yaml",       # what the simulator reads its overrides from
    "monitor_resources.py",     # what the run measures itself with
    "job-0.params.yaml",        # a per-job document, matched as a pattern
])
def test_a_file_the_run_owns_may_not_be_staged_over(tmp_path, reserved):
    with pytest.raises(ValueError, match="the run itself owns"):
        _compose(tmp_path, reserved)


def test_the_scenario_file_is_reserved_too(tmp_path):
    """Not a fixed name: it is whatever this campaign's `execution.scenario_file` is.

    Staged by BASENAME, which is why a scenario nested in ``scenarios/`` still collides with
    a source called ``trial.osc`` at the campaign root.
    """
    with pytest.raises(ValueError, match="the run itself owns"):
        _compose(tmp_path, "trial.osc", scenario_rel="scenarios/trial.osc")


def test_only_the_mount_root_is_contested(tmp_path):
    """The run writes nothing into a subdirectory, so a campaign owns those names."""
    data = _compose(tmp_path, "nav2/entrypoint.sh")
    assert any(rel == "nav2/entrypoint.sh"
               for rel, _ in data["configs"][0]["_config_files"])


def test_an_ordinary_path_is_untouched(tmp_path):
    data = _compose(tmp_path, "files/nav2_params.yaml")
    assert any(rel == "files/nav2_params.yaml"
               for rel, _ in data["configs"][0]["_config_files"])
