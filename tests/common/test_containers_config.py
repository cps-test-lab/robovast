# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``execution.containers``: the schema, the role→container map, and the v1 cut."""

import pytest

from robovast.common.config import validate_config
from robovast.common.containers import plan_containers
from robovast.common.execution import resolve_robovast_image
from robovast.service.image_build import extract_build_specs


def _cfg(**containers):
    return {"version": 2, "execution": {"containers": containers, "runs": 1}}


# -- the schema --------------------------------------------------------------------

def test_a_single_container_is_the_minimal_campaign():
    c = validate_config(_cfg(scenario={"image": "ghcr.io/x/y:1"}))
    assert c.execution.containers["scenario"].image == "ghcr.io/x/y:1"


def test_packages_are_declared_on_the_container_that_gets_them():
    c = validate_config(_cfg(
        scenario={"image": "base:1"},
        sut={"image": "nav2-vendor:humble",
             "system_packages": ["ros-humble-navigation2"],
             "python_packages": ["mujoco>=3.0", ["wheels/a.whl", "wheels/b.whl"]]}))
    sut = c.execution.containers["sut"]
    assert sut.builds_image()
    assert sut.python_packages[0] == "mujoco>=3.0"
    assert len(sut.python_packages[1]) == 2


def test_an_ad_hoc_container_must_name_its_image():
    """Only the known roles have a default; anything else nobody can guess."""
    with pytest.raises(ValueError, match="must state an 'image'"):
        validate_config(_cfg(scenario={"image": "a"}, recorder={"command": ["rec"]}))


def test_the_scenario_container_may_omit_its_image():
    """Omitting it means "the framework's own image", as an absent ``build.base_image``
    used to. Whether that resolves depends on ROBOVAST_IMAGE, which the schema cannot
    see -- ``resolve_robovast_image`` makes that call at run time, and already refuses
    to fall back to a mutable default tag."""
    c = validate_config(_cfg(scenario={"system_packages": ["ros-jazzy-nav2-bringup"]}))
    assert c.execution.containers["scenario"].image is None


def test_only_the_simulation_block_takes_a_backend():
    with pytest.raises(ValueError, match="only the 'simulation' container has"):
        validate_config(_cfg(scenario={"image": "a", "backend": "robosito"}))


def test_unknown_keys_are_rejected_on_an_ordinary_container():
    with pytest.raises(ValueError, match="unknown keys: wrold"):
        validate_config(_cfg(scenario={"image": "a", "wrold": "x"}))


def test_a_backend_owns_the_keys_it_does_not_declare():
    """A simulator backend's own vocabulary rides alongside ``backend`` — RoboVAST
    cannot know it, and validating it away would make every backend edit a schema
    change."""
    cfg = _cfg(scenario={"image": "a"},
               simulation={"backend": "robosito", "config": "worlds/depot.yaml"})
    cfg["execution"]["mode"] = "ros2"
    c = validate_config(cfg)
    assert c.execution.containers["simulation"].model_extra["config"] == "worlds/depot.yaml"


def test_backend_shaped_keys_without_a_backend_are_still_typos():
    with pytest.raises(ValueError, match="no 'backend' to validate them against"):
        validate_config(_cfg(scenario={"image": "a"},
                             simulation={"image": "s", "config": "w.yaml"}))


@pytest.mark.parametrize("entry, match", [
    ([[]], "empty install group"),
    ([["a", "  "]], r"entry 0, item 1 is blank"),
    ([""], r"entry 0 is blank"),
])
def test_python_packages_reject_empty_specs_and_groups(entry, match):
    """Emptiness is what the ``str | list[str]`` annotation cannot catch on its own."""
    with pytest.raises(ValueError, match=match):
        validate_config(_cfg(scenario={"image": "a", "python_packages": entry}))


def test_a_group_is_flat():
    """A group is one pip invocation, so it cannot contain another group. The
    annotation rejects it — no second opinion in the validator."""
    with pytest.raises(ValueError, match="python_packages"):
        validate_config(_cfg(scenario={"image": "a",
                                       "python_packages": [["a", ["b"]]]}))


def test_mode_auto_is_refused_with_a_backend():
    """``auto`` is resolved inside the container by testing for ros2 on PATH, so the
    same .vast would get a different topology in a different image — silently."""
    cfg = _cfg(scenario={"image": "a"}, simulation={"backend": "robosito"})
    with pytest.raises(ValueError, match="must be 'ros2' or 'base'"):
        validate_config(cfg)
    cfg["execution"]["mode"] = "base"
    validate_config(cfg)


# -- resources ---------------------------------------------------------------------

@pytest.mark.parametrize("cpu", [4, 0.5, 4.75, "500m", "0.25"])
def test_cpu_takes_fractional_cores_and_millicores(cpu):
    """On the cluster a campaign's throughput is ``quota // pod_request``, so rounding a
    measured 0.3-core sidecar up to a whole core is paid on every job of the sweep."""
    c = validate_config(_cfg(scenario={"image": "a", "resources": {"cpu": cpu}}))
    assert c.execution.containers["scenario"].resources.cpu == cpu


def test_a_whole_core_stays_an_int():
    """Both lanes render the value with ``str()``. Coercing 4 to 4.0 would rewrite every
    existing campaign's manifest from "4" to "4.0" for no reason."""
    c = validate_config(_cfg(scenario={"image": "a", "resources": {"cpu": 4}}))
    assert isinstance(c.execution.containers["scenario"].resources.cpu, int)


@pytest.mark.parametrize("cpu", ["4Gi", "lots", "", -1])
def test_cpu_that_is_not_a_cpu_quantity_is_refused_here(cpu):
    """Now that ``cpu`` accepts strings, the annotation alone would pass "4Gi" through to
    the manifest — where it surfaces as a pod that never schedules, far from the line
    that caused it."""
    with pytest.raises(ValueError, match="is not a CPU quantity"):
        validate_config(_cfg(scenario={"image": "a", "resources": {"cpu": cpu}}))


def test_a_per_cluster_cpu_list_is_checked_entry_by_entry():
    """The per-cluster form is where a bad value hides longest: only the cluster whose
    entry is wrong ever fails, and only once it is the active context."""
    with pytest.raises(ValueError, match="is not a CPU quantity"):
        validate_config(_cfg(scenario={"image": "a", "resources": {
            "cpu": [{"gcp-c4": "500m"}, {"local": "8Gi"}]}}))


# -- the v1 cut --------------------------------------------------------------------

def test_version_1_is_refused_with_instructions():
    """There is no migration tool and no v1 reader, so this message IS the migration
    path. Assert it says where each removed key went, not merely that it failed."""
    with pytest.raises(ValueError) as excinfo:
        validate_config({"version": 1, "execution": {"image": "x", "runs": 1}})
    text = str(excinfo.value)
    assert "execution.containers.scenario.image" in text
    assert "secondary_containers" in text
    assert "build:" in text
    assert "version: 2" in text


# -- the role→container map --------------------------------------------------------

def test_three_containers_when_each_role_is_its_own():
    plan = plan_containers({"containers": {
        "scenario": {"image": "runner"},
        "simulation": {"image": "rst-ros", "command": ["rst", "sim", "w.yaml", "--ros"]},
        "sut": {"image": "nav2"},
    }})
    assert plan.names() == ["scenario", "simulation", "sut"]
    assert plan.main.name == "scenario"
    assert [c.name for c in plan.sidecars] == ["simulation", "sut"]
    assert plan.by_name("simulation").command == ["rst", "sim", "w.yaml", "--ros"]


def test_a_stepped_simulator_is_the_scenario_container():
    """It has to be: a SimulationInterface is stepped in-process. The *name* still
    resolves, so a caller never needs to know which shape it is looking at."""
    plan = plan_containers({"containers": {
        "scenario": {"image": "combined"},
        "simulation": {"python_packages": ["./my_plugins"]},
    }})
    assert plan.names() == ["scenario"]
    assert plan.by_name("simulation").name == "scenario"
    assert plan.main.roles == ("scenario", "simulation")


def test_extending_a_folded_simulation_reaches_the_container_it_runs_in():
    """Otherwise a campaign's own simulator plugins would be silently dropped for
    exactly the shape that needs them most."""
    plan = plan_containers({"containers": {
        "scenario": {"image": "combined", "python_packages": ["./base"]},
        "simulation": {"python_packages": ["./my_plugins"]},
    }})
    assert plan.main.builds
    assert plan.main.python_packages == ("./base", "./my_plugins")


def test_a_stack_that_bundles_its_own_simulator_answers_to_simulation():
    plan = plan_containers({"containers": {"scenario": {"image": "a"}, "sut": {"image": "gz"}}})
    assert plan.by_name("simulation").name == "sut"


def test_an_unknown_name_lists_what_there_is():
    """The caller is usually a person or an agent choosing one; "no such container"
    without the list is a second round trip."""
    plan = plan_containers({"containers": {"scenario": {"image": "a"}}})
    with pytest.raises(KeyError, match="it has: scenario"):
        plan.by_name("simulation")


# -- images ------------------------------------------------------------------------

def test_one_build_per_container_that_adds_packages():
    cfg = validate_config(_cfg(
        scenario={"image": "runner"},
        sut={"image": "nav2", "system_packages": ["ros-humble-navigation2"]}))
    specs = extract_build_specs(cfg)
    assert set(specs) == {"sut"}
    # The tag is the container's name and the base is its declared image: an author
    # states what a container adds, never what it adds to.
    assert specs["sut"].tag == "sut"
    assert specs["sut"].base_image == "nav2"


def test_a_container_with_no_packages_builds_nothing():
    cfg = validate_config(_cfg(scenario={"image": "runner"}, sut={"image": "nav2"}))
    assert extract_build_specs(cfg) == {}


def test_a_built_image_is_substituted_by_name():
    plan = plan_containers(
        {"containers": {"scenario": {"image": "base"},
                        "sut": {"image": "nav2", "system_packages": ["x"]}}},
        images={"sut": "registry/sut@sha256:abc"})
    assert plan.by_name("sut").image == "registry/sut@sha256:abc"
    assert plan.main.image == "base"


def test_an_explicit_image_addresses_the_scenario_container_only():
    """A single ``--image`` flag can only mean the container the scenario runs in."""
    plan = plan_containers(
        {"containers": {"scenario": {"image": "base"}, "sut": {"image": "nav2"}}},
        explicit_main="override:1")
    assert plan.main.image == "override:1"
    assert plan.by_name("sut").image == "nav2"


def test_a_sidecar_has_no_image_fallback():
    """Guessing an image for the system under test would run something nobody named."""
    with pytest.raises(ValueError, match="no image for container 'sut'"):
        plan_containers({"containers": {"scenario": {"image": "a"}, "sut": {}}},
                        main_image_fallback="default:1")


def test_run_image_required_fails_loud(monkeypatch):
    # The image a campaign RUNS must be pinned: nothing configured -> raise, not
    # silently use the mutable default tag.
    #
    # CampaignConfigError specifically, and asserted as such: the message is
    # self-contained and actionable, so `failure_detail` must report it WITHOUT a
    # traceback. As a plain ValueError it reached the worker's catch-all and printed a
    # stack trace through kubernetes_backend and controller, which reads as a RoboVAST
    # bug rather than as a .vast key the author has to set.
    from robovast.common.errors import CampaignConfigError

    monkeypatch.delenv("ROBOVAST_IMAGE", raising=False)
    with pytest.raises(CampaignConfigError,
                       match="no container image configured for this run") as excinfo:
        resolve_robovast_image(required=True)
    assert excinfo.value.include_traceback is False
    assert resolve_robovast_image(required=True, explicit="reg/x:1") == "reg/x:1"
    assert resolve_robovast_image(required=True, config_image="reg/y:2") == "reg/y:2"


# -- reading a section out of an old snapshot ---------------------------------------

def test_a_section_can_be_read_from_a_version_1_snapshot(tmp_path):
    """Postprocessing discovers a ``.vast`` from the *shared* results directory, which
    also holds older campaigns' archived snapshots. Validating the whole document just to
    read ``results_processing`` meant one stale neighbour stopped a perfectly good v2
    campaign from producing its results -- the run had already succeeded.

    Sections did not change between versions, so a reader of one is not entitled to an
    opinion about the rest of the file.
    """
    from robovast.common.common import load_config

    old = tmp_path / "old.vast"
    old.write_text(
        "version: 1\n"
        "execution: {image: ghcr.io/x/y:1, runs: 1}\n"
        "results_processing:\n"
        "  postprocessing: [rosbags_to_csv]\n")

    assert load_config(str(old), subsection="results_processing", allow_missing=True) == {
        "postprocessing": ["rosbags_to_csv"]}


def test_loading_a_whole_version_1_config_still_refuses(tmp_path):
    """The leniency is scoped to a section. A caller loading the whole config is about to
    *run* it, which is exactly where an unsupported version matters."""
    import pytest

    from robovast.common.common import load_config

    old = tmp_path / "old.vast"
    old.write_text("version: 1\nexecution: {image: ghcr.io/x/y:1, runs: 1}\n")
    with pytest.raises(ValueError, match="config version 1 is no longer supported"):
        load_config(str(old))


def test_a_campaigns_own_config_wins_over_a_neighbours(tmp_path):
    """Postprocessing passes a campaign directory, and that campaign's own snapshot is
    the only correct answer for it.

    The scan returns the lexicographically *last* campaign in a results root, which is
    not even the newest -- a name is ``<experiment>-<timestamp>``, so it sorts by
    experiment first. Reaching it from a campaign directory meant postprocessing read a
    different experiment's ``results_processing`` config, silently.
    """
    from robovast.common.results_utils import find_campaign_vast_file

    mine = tmp_path / "aaa-experiment-2026-08-07-000001"
    other = tmp_path / "zzz-experiment-2026-08-04-000001"
    for d, body in ((mine, "version: 2\n"), (other, "version: 1\n")):
        (d / "_config").mkdir(parents=True)
        (d / "_config" / "campaign.vast").write_text(body)

    # Given the campaign, its own config -- not the neighbour the scan would reach.
    found, _ = find_campaign_vast_file(str(mine))
    assert found == str(mine / "_config" / "campaign.vast")

    # Given the root, the scan still answers, for the caller that only wants "some" config.
    found_root, _ = find_campaign_vast_file(str(tmp_path))
    assert found_root == str(other / "_config" / "campaign.vast")
