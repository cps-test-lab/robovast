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
    """Omitting it means "the RoboVAST framework image", which is the normal case.

    Which project and tag that resolves to is the deployment's to choose and is not
    visible to a schema; ``resolve_robovast_image`` makes the call at run time.
    """
    c = validate_config(_cfg(scenario={"system_packages": ["ros-jazzy-nav2-bringup"]}))
    assert c.execution.containers["scenario"].image is None


def test_only_the_simulation_block_takes_a_backend():
    with pytest.raises(ValueError, match="only the 'simulation' container has"):
        validate_config(_cfg(scenario={"image": "a", "backend": "roqsim"}))


def test_unknown_keys_are_rejected_on_an_ordinary_container():
    with pytest.raises(ValueError, match="unknown keys: wrold"):
        validate_config(_cfg(scenario={"image": "a", "wrold": "x"}))


def test_a_backend_owns_the_keys_it_does_not_declare():
    """A simulator backend's own vocabulary rides alongside ``backend`` — RoboVAST
    cannot know it, and validating it away would make every backend edit a schema
    change."""
    cfg = _cfg(scenario={"image": "a"},
               simulation={"backend": "roqsim", "config": "worlds/depot.yaml"})
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
    cfg = _cfg(scenario={"image": "a"}, simulation={"backend": "roqsim"})
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

def test_authoring_an_old_version_is_refused_with_instructions():
    """A migration tool now exists, so the refusal must name it *and* still explain the
    restructuring.

    This test used to assert the opposite premise -- that the message was the only
    migration path, because there was neither a tool nor a v1 reader. Both now exist
    (``robovast.common.migrations``), so what matters here changed: the one-command fix has
    to be in the text, and the key-by-key explanation has to survive alongside it, because
    someone authoring a file still needs to know where each removed key went.
    ``validate_config`` remains the STRICT policy -- reading an archived campaign is
    ``load_config(upgrade=True)``, covered separately."""
    with pytest.raises(ValueError) as excinfo:
        validate_config({"version": 1, "execution": {"image": "x", "runs": 1}})
    text = str(excinfo.value)
    assert "vast configuration upgrade" in text
    assert "execution.containers.scenario.image" in text
    assert "secondary_containers" in text
    assert "build:" in text


# -- the role→container map --------------------------------------------------------

def test_three_containers_when_each_role_is_its_own():
    plan = plan_containers({"containers": {
        "scenario": {"image": "runner"},
        "simulation": {"image": "roqsim-ros", "command": ["roqsim", "sim", "w.yaml", "--ros"]},
        "sut": {"image": "nav2"},
    }})
    assert plan.names() == ["scenario", "simulation", "sut"]
    assert plan.main.name == "scenario"
    assert [c.name for c in plan.sidecars] == ["simulation", "sut"]
    assert plan.by_name("simulation").command == ["roqsim", "sim", "w.yaml", "--ros"]


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


def test_a_container_with_no_family_default_fails_loud(monkeypatch):
    # A container RoboVAST does not own -- a sidecar, a system-under-test -- has no
    # default: guessing the framework image for it would launch something nobody named.
    # (The main container DOES have one; see the test below. That is the difference
    # `fallback` expresses, and it used to be tangled up with refusing a mutable tag.)
    #
    # CampaignConfigError specifically, and asserted as such: the message is
    # self-contained and actionable, so `failure_detail` must report it WITHOUT a
    # traceback. As a plain ValueError it reached the worker's catch-all and printed a
    # stack trace through kubernetes_backend and controller, which reads as a RoboVAST
    # bug rather than as a .vast key the author has to set.
    from robovast.common.errors import CampaignConfigError

    with pytest.raises(CampaignConfigError,
                       match="no image configured for this") as excinfo:
        resolve_robovast_image(fallback=False)
    assert excinfo.value.include_traceback is False
    assert resolve_robovast_image(fallback=False, explicit="reg/x:1") == "reg/x:1"
    assert resolve_robovast_image(fallback=False, config_image="reg/y:2") == "reg/y:2"


def test_the_main_container_falls_back_to_the_family(monkeypatch):
    """A campaign that names no image runs the framework image, and says which.

    This is the amended rule. Refusing outright -- the old behaviour -- made every
    ``.vast`` carry a pinned ref, which is what put five registry-specific strings into
    a shipped example and a published dataset. What actually makes a run reproducible is
    the digest recorded *from* the run, not a tag copied into the config by hand; so the
    unpinned case resolves and warns instead of refusing.
    """
    monkeypatch.setenv("ROBOVAST_PROJECT", "example.test/ns")
    monkeypatch.delenv("ROBOVAST_PROJECT_TAG", raising=False)
    assert resolve_robovast_image() == "example.test/ns/robovast:latest"
    # An explicit value still wins over the family, byte for byte -- digest included.
    pinned = "harbor.example/robovast@sha256:" + "a" * 64
    assert resolve_robovast_image(config_image=pinned) == pinned


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

    from robovast.common.common import load_config

    old = tmp_path / "old.vast"
    old.write_text("version: 1\nexecution: {image: ghcr.io/x/y:1, runs: 1}\n")
    with pytest.raises(ValueError, match="not the current version"):
        load_config(str(old))

    # ...and the third policy is the way to read one anyway: opt in explicitly, get the
    # config laddered in memory, and leave the file on disk untouched. Without this an
    # archived campaign stops being readable by the tool that produced it.
    before = old.read_text()
    upgraded = load_config(str(old), upgrade=True)
    assert upgraded["version"] == 2
    assert upgraded["execution"]["containers"]["scenario"]["image"] == "ghcr.io/x/y:1"
    assert old.read_text() == before


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


# ---------------------------------------------------------------------------
# family: refs must be resolved before a build spec, and never reach a FROM
# ---------------------------------------------------------------------------

def test_a_backend_contributed_family_ref_is_resolved_for_the_build(tmp_path, monkeypatch):
    """The build path has to resolve `family:` refs, not just the composition path.

    A backend names its member symbolically, because which project/tag it comes from is a
    property of the campaign and does not exist when the backend runs. config_generation
    resolved that immediately; extract_build_specs did not -- and the gap was asymmetric,
    which is what hid it: a container taking the DEFAULT member was resolved on the
    composition path, so `sut` and `scenario` built correctly while the one container that
    declared a `backend:` carried `family:robovast-roqsim` into its Dockerfile FROM. Docker
    read that as repository `family`, tag `robovast-roqsim`, and the campaign died in
    BuildKit with a registry `insufficient_scope` -- three layers from the cause.
    """
    from robovast.common.execution import FAMILY_IMAGE_PREFIX, family_image_ref

    class _Block(dict):
        def model_dump(self):
            return dict(self)

    class _Execution:
        mode = "ros2"
        containers = {"sim": _Block(image=family_image_ref("robovast-roqsim"),
                                    python_packages=["./"])}

    class _Config:
        execution = _Execution()

    specs = extract_build_specs(_Config(), base_dir=str(tmp_path),
                                image_project="example.org/team", image_project_tag="v9")
    assert specs, "a container adding python_packages must produce a spec"
    base = specs["sim"].base_image
    assert not base.startswith(FAMILY_IMAGE_PREFIX), f"left unresolved: {base}"
    assert base == "example.org/team/robovast-roqsim:v9", base


@pytest.mark.parametrize("ref", ["family:robovast-roqsim", "build:scenario"])
def test_an_unresolved_ref_cannot_reach_a_dockerfile(tmp_path, ref):
    """The hard error both prefixes promise, which nothing implemented.

    `execution.py` says a ref reaching a pod or compose spec unresolved is "a hard error
    rather than an image name nothing can pull" -- but `is_family_image_ref` was never called
    outside that module, so a Dockerfile happily got one. Failing here names the bug; failing
    in BuildKit names a registry.
    """
    from robovast.service.image_build import BuildSpec, generate_dockerfile

    spec = BuildSpec(tag="sim", python_packages=["./"])
    with pytest.raises(ValueError, match="unresolved image ref"):
        generate_dockerfile(spec, tmp_path, ref)
