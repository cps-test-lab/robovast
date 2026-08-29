# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A stack-varying campaign, from ``.vast`` to the files each cell actually runs.

The unit tests pin the pieces; this pins the seam between them -- that a factor on the
``sut:`` channel survives composition, that each cell carries its own rewritten copy of the
stack's configuration, and that the campaign's own file is never the one that changed.
"""

import os
import textwrap

import pytest
import yaml

from robovast.common.config_generation import generate_scenario_variations

_SCENARIO = """\
import osc.robotics

scenario nav:
    params_file: string = ''
    do serial:
        wait elapsed(1s)
"""

_PARAMS = """\
local_costmap:
  local_costmap:
    ros__parameters:
      plugins: ["obstacle_layer", "inflation_layer"]
      inflation_layer:
        inflation_radius: 0.55
      voxel_layer:
        enabled: true
"""

_BT = """\
<root BTCPP_format="4">
  <RecoveryNode number_of_retries="6" name="NavigateRecovery"/>
  <RecoveryNode number_of_retries="1" name="ComputePathToPose"/>
</root>
"""

_BASE = "nav2.local_costmap.local_costmap.ros__parameters"


def _project(tmp_path, configuration, run_files=""):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    files = tmp_path / "files"
    files.mkdir()
    (files / "nav2_params.yaml").write_text(_PARAMS)
    (files / "nav2_bt.xml").write_text(_BT)
    # The caller's block is dedented and re-indented here rather than interpolated raw:
    # a `.vast` is whitespace-significant and every caller would otherwise have to know
    # this template's indentation.
    block = textwrap.indent(textwrap.dedent(configuration).strip("\n"), "  ")
    vast = tmp_path / "campaign.vast"
    vast.write_text(
        "version: 3\n"
        "metadata: {name: sut-channel}\n"
        "configuration:\n"
        f"{block}\n"
        "execution:\n"
        "  containers:\n"
        "    scenario: {image: scen:latest}\n"
        "    sut:\n"
        "      image: sut:latest\n"
        "      config_files:\n"
        "        nav2: files/nav2_params.yaml\n"
        "        bt: files/nav2_bt.xml\n"
        "  runs: 1\n"
        "  scenario_file: scenario.osc\n"
        f"{run_files}\n")
    return str(vast)


def _compose(tmp_path, configuration, run_files=""):
    return generate_scenario_variations(
        variation_file=_project(tmp_path, configuration, run_files),
        output_dir=str(tmp_path / "out"), use_cache=False, isolate_plugins=False)


def _written(config, rel):
    """The absolute path of the copy staged for *config*."""
    for staged_rel, path in config["_config_files"]:
        if staged_rel == rel:
            return path
    raise AssertionError(f"{rel} was not staged for {config['name']}: {config['_config_files']}")


def test_a_factor_gives_each_cell_its_own_stack_configuration(tmp_path):
    data = _compose(tmp_path, f"""\
        - name: inflation
          variations:
          - ParameterVariationList:
              sut: {_BASE}.inflation_layer.inflation_radius
              values: [0.30, 0.55]
    """)
    configs = data["configs"]
    assert len(configs) == 2

    radii = []
    for config in configs:
        path = _written(config, "files/nav2_params.yaml")
        params = yaml.safe_load(open(os.path.join(data["_output_dir"], path)
                                     if not os.path.isabs(path) else path, encoding="utf-8"))
        radii.append(params["local_costmap"]["local_costmap"]["ros__parameters"]
                     ["inflation_layer"]["inflation_radius"])
    assert sorted(radii) == [0.30, 0.55]

    # the resolved block is recorded on the configuration, as the other channels record theirs
    assert {c["sut"][f"{_BASE}.inflation_layer.inflation_radius"] for c in configs} == {0.30, 0.55}


def test_the_campaigns_own_file_is_never_the_one_that_changed(tmp_path):
    """A cell runs a copy. If the source were edited in place, the second cell would compose
    against the first one's values and no one would see it."""
    _compose(tmp_path, f"""\
        - name: inflation
          variations:
          - ParameterVariationList:
              sut: {_BASE}.inflation_layer.inflation_radius
              values: [0.30, 0.55]
    """)
    original = yaml.safe_load((tmp_path / "files" / "nav2_params.yaml").read_text())
    assert original["local_costmap"]["local_costmap"]["ros__parameters"][
        "inflation_layer"]["inflation_radius"] == 0.55


def test_two_formats_vary_in_one_configuration(tmp_path):
    """The point of formats being plugins: a YAML params file and an XML behaviour tree are
    two factors of one campaign, and neither grammar is RoboVAST's."""
    data = _compose(tmp_path, f"""\
        - name: mixed
          variations:
          - ParameterVariationList:
              sut: {_BASE}.plugins
              values:
              - ["obstacle_layer", "inflation_layer"]
              - ["obstacle_layer", "voxel_layer", "inflation_layer"]
          - ParameterVariationList:
              sut: bt.//RecoveryNode[@name='NavigateRecovery']/@number_of_retries
              values: [2, 6]
    """)
    assert len(data["configs"]) == 4
    for config in data["configs"]:
        for rel in ("files/nav2_params.yaml", "files/nav2_bt.xml"):
            _written(config, rel)


def test_a_fixed_block_is_overridden_by_a_factor(tmp_path):
    """The precedence the other two channels have."""
    data = _compose(tmp_path, f"""\
        - name: fixed-and-varied
          sut:
            {_BASE}.inflation_layer.inflation_radius: 0.10
          variations:
          - ParameterVariationList:
              sut: {_BASE}.inflation_layer.inflation_radius
              values: [0.30]
    """)
    config = data["configs"][0]
    assert config["sut"][f"{_BASE}.inflation_layer.inflation_radius"] == 0.30


def test_absence_reaches_the_file_the_cell_runs(tmp_path):
    data = _compose(tmp_path, f"""\
        - name: no-voxel
          sut:
            {_BASE}.voxel_layer: {{$absent: true}}
    """)
    config = data["configs"][0]
    path = _written(config, "files/nav2_params.yaml")
    full = path if os.path.isabs(path) else os.path.join(data["_output_dir"], path)
    params = yaml.safe_load(open(full, encoding="utf-8"))
    assert "voxel_layer" not in params["local_costmap"]["local_costmap"]["ros__parameters"]


def test_a_misspelled_destination_is_refused_before_anything_runs(tmp_path):
    with pytest.raises(Exception, match="addresses nothing|does not declare"):
        _compose(tmp_path, """\
        - name: typo
          variations:
          - ParameterVariationList:
              sut: nav2.local_costmp.inflation_radius
              values: [0.30, 0.55]
    """)


def test_a_source_also_in_run_files_is_refused(tmp_path):
    """Two copies in the container, and if the stack reads the un-rewritten one then every
    cell ran the same configuration and nothing said so."""
    with pytest.raises(Exception, match="run_files"):
        _compose(tmp_path, f"""\
        - name: clash
          variations:
          - ParameterVariationList:
              sut: {_BASE}.inflation_layer.inflation_radius
              values: [0.30, 0.55]
    """, run_files="  run_files: [files/nav2_params.yaml]")


def test_the_environment_carrier_refuses_rather_than_doing_nothing(tmp_path):
    """It is the channel's second carrier and no lane delivers it per configuration yet.
    Silently dropping it would be a campaign whose factor did not vary."""
    with pytest.raises(Exception, match="not delivered per configuration yet"):
        _compose(tmp_path, """\
        - name: env
          variations:
          - ParameterVariationList:
              sut: env.NAV2_PROFILE
              values: [conservative, aggressive]
    """)
