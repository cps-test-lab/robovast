# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Where a run's ``ground_truth_poses`` come from is its simulator backend's answer.

A backend whose simulator records its own state names the table; one that records nothing --
and a campaign with no backend at all, whose scenario launches its simulator -- leaves the ROS
convention, a ``*_gt`` frame on ``/tf``. The answer reaches the decoder through the campaign's
decoder configuration, so a copy of the campaign reads the same ground truth anywhere.
"""

import pytest
import yaml

from robovast.common.simulators import SimulatorBackend, ground_truth_source
from robovast.results_processing.campaign_tables import write_decoder_config
from robovast_decode.ground_truth import source_of
from robovast_decode.layout import decoder_config

TRUTH = {"table": "stage_poses", "entity_kind": "robot"}


class _Recording(SimulatorBackend):
    def ground_truth(self, cfg, execution):
        return dict(TRUTH)


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    import robovast.common.simulators as mod
    backends = {"recording": _Recording, "silent": SimulatorBackend}
    monkeypatch.setattr(mod, "resolve_backend", lambda name, base_dir="": backends[name]())


def _execution(backend=None):
    block = {"backend": backend} if backend else {"image": "sim:1"}
    return {"mode": "ros2", "runs": 1, "containers": {"simulation": block}}


def test_a_recording_backend_names_its_table():
    assert ground_truth_source(_execution("recording")) == TRUTH


@pytest.mark.parametrize("backend", ["silent", None])
def test_no_answer_is_the_ros_convention(backend):
    assert ground_truth_source(_execution(backend)) is None


def test_roqsim_reads_the_robots_in_its_own_recording():
    from robovast_sim_roqsim.backend import RoqsimBackend
    assert RoqsimBackend().ground_truth(None, {}) == {"table": "sim_poses",
                                                     "entity_kind": "robot"}
    assert source_of({"ground_truth": RoqsimBackend().ground_truth(None, {})})


def _campaign(tmp_path, backend):
    campaign = tmp_path / "camp"
    (campaign / "_config").mkdir(parents=True)
    vast = campaign / "_config" / "campaign.vast"
    vast.write_text(yaml.safe_dump({"version": 7, "execution": _execution(backend)}))
    return campaign, vast


def test_the_decoder_configuration_carries_the_backends_answer(tmp_path):
    campaign, vast = _campaign(tmp_path, "recording")
    write_decoder_config(str(campaign), str(vast))
    assert decoder_config(str(campaign))["ground_truth"] == TRUTH


def test_a_campaign_without_one_leaves_the_default(tmp_path):
    campaign, vast = _campaign(tmp_path, None)
    write_decoder_config(str(campaign), str(vast))
    assert "ground_truth" not in decoder_config(str(campaign))
