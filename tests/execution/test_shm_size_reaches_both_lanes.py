# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``/dev/shm`` sizing, from a real ``.vast`` to what each lane renders.

Deliberately composed rather than hand-fed. The lane tests beside this one build their
``execution`` dict directly, which is why ``shm_size`` could be dropped in composition and
still look covered: the manifest was right about a dict no campaign ever produced. These
start from a file.
"""

import textwrap

import pytest

import yaml

from robovast.common.config import DEFAULT_SHM_SIZE
from robovast.common.config_generation import generate_scenario_variations
from robovast.common.quantity import to_bytes
from robovast.execution.execution_utils.execute_local import _build_packed_compose_yaml
from robovast.execution.packer import JobSpec

from .test_bt_log_placement import _item, _plan

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
        version: 3
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

    The default is settled here, not in each lane, so the two cannot drift apart -- which is
    the whole reason the field existed in every campaign file in the first place.
    """
    assert _composed_execution(tmp_path, declared)["shm_size"] == expected


def _compose(tmp_path, execution):
    return yaml.safe_load(_build_packed_compose_yaml(
        docker_image="img:test", out_path=str(tmp_path), results_dir_var="${RESULTS}",
        job=JobSpec(items=[_item("cfg-a", 0)], index=0), param_file_rel="p.yaml",
        run_files=[], env_vars={}, pre_command=None, post_command=None, uid=1000, gid=1000,
        main_cpu=1, main_memory=None, main_gpu=False, plan=_plan(),
        use_gui_block=False, execution=execution))


@pytest.mark.parametrize("declared", [None, "2Gi"])
def test_the_local_lane_sizes_the_main_container_in_bytes(tmp_path, declared):
    """The sidecars join this container's IPC namespace, so its size is the run's size.

    Asserted as an INTEGER number of bytes, not as the string the ``.vast`` used, because
    the two lanes do not share a quantity language: Kubernetes takes ``512Mi`` and Compose
    refuses it ("invalid suffix: 'mi'"). An earlier version of this test compared against
    our own output instead of against something Compose would accept, so it passed while
    every local run the value reached would have died before starting a container.
    """
    service = _compose(tmp_path, _composed_execution(tmp_path, declared))["services"]["robovast"]
    expected = to_bytes(declared or DEFAULT_SHM_SIZE)

    assert service["shm_size"] == expected
    assert isinstance(service["shm_size"], int)
