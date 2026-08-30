# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The system-under-test channel: how the stack is configured belongs to a CONFIGURATION.

Not per campaign, and not per run -- repetitions of a configuration share a configuration
of the stack, which is what makes them repetitions. These tests pin the properties that
make the channel a peer of the other two rather than an escape hatch: a destination names a
surface whose owner can check it, a block stays flat because the path belongs to the file's
format, absence is expressible, and nothing can leave an un-rewritten copy of a source
beside the rewritten one.
"""

import os

import pytest
import yaml

from robovast.common.config import RESERVED_ENV_NAMES
from robovast.common.sut_channel import (ABSENT, SutChannelError, check_destinations,
                                         declared_sources, is_absent, materialize,
                                         merge_sut_block, resolve_sut_path,
                                         split_destination)

PARAMS = """local_costmap:
  local_costmap:
    ros__parameters:
      plugins: ["obstacle_layer", "inflation_layer"]
      inflation_layer:
        inflation_radius: 0.55
      voxel_layer:
        enabled: true
"""

BT = """<root BTCPP_format="4">
  <RecoveryNode number_of_retries="6" name="NavigateRecovery"/>
  <RecoveryNode number_of_retries="1" name="ComputePathToPose"/>
</root>
"""


@pytest.fixture(name="campaign")
def _campaign(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "nav2_params.yaml").write_text(PARAMS, encoding="utf-8")
    (files / "nav2_bt.xml").write_text(BT, encoding="utf-8")
    execution = {
        "containers": {
            "sut": {"config_files": {"nav2": "files/nav2_params.yaml",
                                     "bt": "files/nav2_bt.xml"}},
            "simulation": {"backend": "stub"},
        }
    }
    return execution, str(tmp_path)


# --- what a destination names -----------------------------------------------------------

def test_a_destination_is_split_exactly_once():
    """Everything after the source name belongs to the format, XPath included."""
    assert split_destination("nav2.local_costmap.ros__parameters.plugins") == (
        "nav2", "local_costmap.ros__parameters.plugins")
    assert split_destination("bt.//RecoveryNode[@name='x']/@retries") == (
        "bt", "//RecoveryNode[@name='x']/@retries")
    assert split_destination("env.NAV2_PROFILE") == ("env", "NAV2_PROFILE")


@pytest.mark.parametrize("bad", ["nav2", "", ".a", "nav2."])
def test_a_destination_without_a_source_and_a_path_is_refused(bad):
    with pytest.raises(SutChannelError, match="<source>.<path>"):
        split_destination(bad)


def test_a_destination_naming_no_declared_source_is_refused_listing_the_real_ones(campaign):
    execution, vast_dir = campaign
    sources = declared_sources(execution, vast_dir)
    with pytest.raises(SutChannelError, match="bt, nav2"):
        resolve_sut_path(sources, "nav3.a.b")


def test_env_resolves_without_being_declared(campaign):
    """The environment is the channel's second carrier, not another kind of document."""
    execution, vast_dir = campaign
    source, path = resolve_sut_path(declared_sources(execution, vast_dir), "env.NAV2_PROFILE")
    assert source is None and path == "NAV2_PROFILE"


# --- who may declare a source -----------------------------------------------------------

@pytest.mark.parametrize("reserved", sorted(RESERVED_ENV_NAMES))
def test_the_env_carrier_cannot_overwrite_what_robovast_sets(reserved):
    """``execution.env`` refuses these, and this carrier reaches the same environment by a
    different route -- so without the guard here the channel would be a way around a rule
    the other route enforces."""
    with pytest.raises(SutChannelError, match="may not override"):
        resolve_sut_path({}, f"env.{reserved}")


def test_the_two_routes_into_the_environment_share_one_reserved_set():
    """Two lists would drift, and the drift is silent: a name guarded on one route and not
    the other reads as guarded everywhere."""
    from robovast.common.config import ExecutionConfig
    with pytest.raises(ValueError, match="reserved"):
        ExecutionConfig(containers={}, runs=1, env=[{"CAMPAIGN_ID": "x"}])


@pytest.mark.parametrize("container,owner", [("simulation", "sim:"), ("scenario", "scenario:")])
def test_a_source_on_a_container_another_channel_owns_is_refused(tmp_path, container, owner):
    """Two channels addressing one surface is how they come to disagree."""
    execution = {"containers": {container: {"config_files": {"x": "files/x.yaml"}}}}
    with pytest.raises(SutChannelError, match=owner):
        declared_sources(execution, str(tmp_path))


def test_every_defined_role_but_sut_is_refused_a_source(tmp_path):
    """Driven off the role set, not off the two names that exist today.

    The test above names ``simulation`` and ``scenario`` because it checks their messages;
    this one is the rule. A defined role added later arrives here refused -- what it must be
    until whichever channel owns its configuration says otherwise -- instead of being
    permitted by a list nobody remembered to extend.
    """
    from robovast.common.config import CONTAINER_ROLES, SUT_CONTAINER
    for role in CONTAINER_ROLES:
        execution = {"containers": {role: {"config_files": {"x": "files/x.yaml"}}}}
        if role == SUT_CONTAINER:
            assert declared_sources(execution, str(tmp_path))["x"].container == role
            continue
        with pytest.raises(SutChannelError, match="system under test"):
            declared_sources(execution, str(tmp_path))


def test_an_ad_hoc_container_beside_the_sut_may_declare_a_source(tmp_path):
    """The refusal is about *defined roles*, not about being other than ``sut``: a stack that
    runs in several containers declares each one's files on the container that reads them."""
    execution = {"containers": {"planner": {"config_files": {"moveit": "files/m.yaml"}}}}
    assert declared_sources(execution, str(tmp_path))["moveit"].container == "planner"


def test_env_cannot_be_declared_as_a_file_source(tmp_path):
    execution = {"containers": {"sut": {"config_files": {"env": "files/x.yaml"}}}}
    with pytest.raises(SutChannelError, match="reserved"):
        declared_sources(execution, str(tmp_path))


def test_a_source_name_used_twice_is_refused(tmp_path):
    """A destination names a source, not a container, so the name must be unambiguous even
    when the system under test spans several containers."""
    execution = {"containers": {
        "sut": {"config_files": {"nav2": "files/a.yaml"}},
        "helper": {"config_files": {"nav2": "files/b.yaml"}}}}
    with pytest.raises(SutChannelError, match="unique across the campaign"):
        declared_sources(execution, str(tmp_path))


# --- the pre-check ------------------------------------------------------------------------

def test_a_destination_addressing_nothing_is_refused_before_anything_runs(campaign):
    execution, vast_dir = campaign
    check_destinations(execution, vast_dir,
                       ["nav2.local_costmap.local_costmap.ros__parameters.plugins",
                        "bt.//RecoveryNode[@name='NavigateRecovery']/@number_of_retries"])
    with pytest.raises(SutChannelError, match="addresses nothing"):
        check_destinations(execution, vast_dir, ["nav2.local_costmp.plugins"])


def test_an_unanswerable_check_warns_rather_than_passing_quietly(campaign, caplog):
    """A skipped check must not look like a check that passed."""
    execution, vast_dir = campaign
    with caplog.at_level("WARNING"):
        check_destinations(execution, vast_dir, ["bt.//[[["])
    assert "was not pre-checked" in caplog.text


# --- resolving and materialising ------------------------------------------------------------

def test_a_variation_value_wins_over_the_fixed_one(campaign):
    base = "nav2.local_costmap.local_costmap.ros__parameters.inflation_layer.inflation_radius"
    assert merge_sut_block({base: 0.55}, {base: 0.30})[base] == 0.30


def test_materialize_writes_one_rewritten_copy_per_configuration(campaign, tmp_path):
    execution, vast_dir = campaign
    out = str(tmp_path / "out")
    base = "nav2.local_costmap.local_costmap.ros__parameters"
    contribution = materialize(execution, vast_dir, {
        f"{base}.inflation_layer.inflation_radius": 0.30,
        f"{base}.plugins": ["obstacle_layer"],
        "bt.//RecoveryNode[@name='NavigateRecovery']/@number_of_retries": 2,
        "env.NAV2_PROFILE": "aggressive",
    }, out, "config1")

    assert contribution.env == {"NAV2_PROFILE": "aggressive"}
    assert {rel for rel, _ in contribution.files} == {"files/nav2_params.yaml",
                                                      "files/nav2_bt.xml"}
    written = dict(contribution.files)
    params = yaml.safe_load(open(written["files/nav2_params.yaml"], encoding="utf-8"))
    layer = params["local_costmap"]["local_costmap"]["ros__parameters"]
    assert layer["inflation_layer"]["inflation_radius"] == 0.30
    assert layer["plugins"] == ["obstacle_layer"]
    assert "<RecoveryNode" in open(written["files/nav2_bt.xml"], encoding="utf-8").read()

    # the campaign's own file is never touched -- the cell runs a copy
    original = yaml.safe_load(open(os.path.join(vast_dir, "files/nav2_params.yaml"),
                                   encoding="utf-8"))
    assert original["local_costmap"]["local_costmap"]["ros__parameters"][
        "inflation_layer"]["inflation_radius"] == 0.55


def test_absence_removes_the_node_rather_than_emptying_it(campaign, tmp_path):
    """A block that is present and empty is not one that is absent, and a stack tells them
    apart -- which is why this is a value and not an assignment of null."""
    execution, vast_dir = campaign
    base = "nav2.local_costmap.local_costmap.ros__parameters"
    contribution = materialize(execution, vast_dir,
                               {f"{base}.voxel_layer": {ABSENT: True}},
                               str(tmp_path / "out"), "config1")
    written = dict(contribution.files)["files/nav2_params.yaml"]
    params = yaml.safe_load(open(written, encoding="utf-8"))
    assert "voxel_layer" not in params["local_costmap"]["local_costmap"]["ros__parameters"]


def test_absence_on_the_environment_means_unset(campaign, tmp_path):
    execution, vast_dir = campaign
    contribution = materialize(execution, vast_dir, {"env.NAV2_PROFILE": {ABSENT: True}},
                               str(tmp_path / "out"), "config1")
    assert contribution.env == {"NAV2_PROFILE": None}


def test_the_absence_marker_is_recognised_narrowly():
    """Strict, so a legitimate mapping that merely mentions the key is still data."""
    assert is_absent({ABSENT: True})
    assert not is_absent({ABSENT: False})
    assert not is_absent({ABSENT: True, "enabled": False})
    assert not is_absent("$absent")
    assert not is_absent(None)
