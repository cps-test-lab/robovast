# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Each check tier says what it could not settle, instead of misreporting it.

Three tools check a ``.vast`` and differ in two things: whether they install the
``plugins:`` block, and whether they compose inside an execution backend's container
context. Neither difference used to be visible in what they reported, so a correct file
looked broken:

* a ``plugins:``-declaring ``.vast`` was told to declare the package in ``plugins:``, and
* a variation needing an auxiliary container died on a bare ``FileNotFoundError: 'docker'``.

The invariant behind both: a name or a runner that is genuinely missing must still fail
loudly. These tests pin the difference between "cannot check that here" and "that is wrong".
"""

import shutil
import sys
from pathlib import Path

import pytest

_VAST = """\
version: 2
metadata:
  name: tiers
{plugins}configuration:
- name: cell
  variations:
  - {variation}:
      foo: 1
execution:
  containers:
    scenario:
      resources:
        cpu: 1
  runs: 2
  scenario_file: scenario.osc
"""


@pytest.fixture(autouse=True)
def _restore_sys_path():
    before = list(sys.path)
    yield
    sys.path[:] = before


def _project(tmp_path, *, declare_plugins, variation="AStagedVariation"):
    """A ``.vast`` naming a variation no installed entry point provides."""
    (tmp_path / "scenario.osc").write_text(
        "scenario test:\n    do serial:\n        wait elapsed(1s)\n")
    plugins = "plugins:\n- myplug\n" if declare_plugins else ""
    vast = tmp_path / "tiers.vast"
    vast.write_text(_VAST.format(plugins=plugins, variation=variation))
    return str(vast)


def _stage_plugin(tmp_path, name="AStagedVariation"):
    """Register *name* the way an installed distribution does: metadata only.

    Written by hand rather than pip-installed so the test stays offline and fast, and so it
    asserts what the production path actually reads -- entry-point metadata in the staged
    dir. The module the entry point points at is deliberately absent: resolving the *name*
    must not require importing it.
    """
    from robovast.common.config_plugins import plugin_site_dir
    dist = Path(plugin_site_dir(str(tmp_path))) / "myplug-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: myplug\nVersion: 0.1.0\n")
    (dist / "entry_points.txt").write_text(
        f"[robovast.variation_types]\n{name} = myplug.nonexistent:{name}\n")


def _problems(vast_path):
    from robovast.common.config_validation import validate_project_file
    return validate_project_file(vast_path)


# -- the plugins tier ----------------------------------------------------------------

def test_staged_plugin_name_resolves_without_importing_it(tmp_path):
    """A staged plugin's variation name is not a problem -- and is not imported to say so."""
    _stage_plugin(tmp_path)
    path = _project(tmp_path, declare_plugins=True)
    report = _problems(path)

    variation_problems = [p for p in report["problems"] if p["stage"] == "variation"]
    assert variation_problems == []
    # The whole point: resolving the name cost no import into this long-lived process.
    assert not any("myplug" in m for m in sys.modules)
    assert not any(".robovast_plugins" in p for p in sys.path)


def test_declared_but_uninstalled_plugin_is_not_told_to_declare_it(tmp_path):
    """The advice that cost two re-uploads: 'declare it in plugins:' when it already is."""
    path = _project(tmp_path, declare_plugins=True)
    report = _problems(path)

    (problem,) = [p for p in report["problems"] if p["stage"] == "variation"]
    message = problem["message"]
    assert "declare that package in the top-level 'plugins:' list" not in message
    # Says what is true, and what settles it.
    assert "preview_configurations" in message
    assert "generation" in message


def test_a_typo_in_a_staged_project_is_reported_as_unknown(tmp_path):
    """A staged project has already composed, so "compose and it will resolve" is false.

    The name it does not provide is genuinely unknown -- almost always a typo -- and the
    generic message serves that better because it lists the names that do exist.
    """
    _stage_plugin(tmp_path, name="AStagedVariation")
    path = _project(tmp_path, declare_plugins=True, variation="AStagedVariatoin")
    report = _problems(path)

    (problem,) = [p for p in report["problems"] if p["stage"] == "variation"]
    assert "Unknown variation class" in problem["message"]
    assert "did not resolve here" not in problem["message"]


def test_undeclared_variation_keeps_the_original_advice(tmp_path):
    """With no ``plugins:`` at all the old message is right -- do not soften it."""
    path = _project(tmp_path, declare_plugins=False)
    report = _problems(path)

    (problem,) = [p for p in report["problems"] if p["stage"] == "variation"]
    assert "Unknown variation class" in problem["message"]
    assert "plugins:" in problem["message"]


def test_staged_names_are_read_from_metadata_only(tmp_path):
    from robovast.common.config_plugins import staged_variation_type_names

    assert staged_variation_type_names(str(tmp_path)) == set()
    _stage_plugin(tmp_path, name="SomeVariation")
    assert staged_variation_type_names(str(tmp_path)) == {"SomeVariation"}


# -- the container tier --------------------------------------------------------------

def _spec():
    from robovast.common.variation.container_runner import ContainerSpec
    return ContainerSpec(image="ghcr.io/example/builder", command_prefix=["build"])


def test_no_runner_and_no_docker_refuses_naming_what_wanted_it(monkeypatch):
    from robovast.common.config_generation import _make_container_runner
    from robovast.common.errors import AuxContainerUnavailable

    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: None)
    with pytest.raises(AuxContainerUnavailable) as excinfo:
        _make_container_runner(_spec(), purpose="variation FloorplanGeneration")

    message = str(excinfo.value)
    assert "FloorplanGeneration" in message       # what needed it
    assert "aux-builder" in message               # which container
    assert "start_campaign" in excinfo.value.next_step
    # The cost, so a caller who only needed the sweep's shape can decline.
    assert "trial" in excinfo.value.next_step


def test_local_docker_still_serves_the_fallback(monkeypatch):
    """The working case: a host with docker previews a container-backed variation."""
    from robovast.common.config_generation import _make_container_runner
    from robovast.common.variation.container_runner import LocalContainerRunner

    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/docker")
    runner = _make_container_runner(_spec(), purpose="variation X")
    try:
        assert isinstance(runner, LocalContainerRunner)
    finally:
        runner.close()


def test_backend_factory_wins_over_the_docker_check(monkeypatch):
    """In-cluster there is no docker in the pod, but there *is* a factory. Must not refuse."""
    from robovast.common.config_generation import (_make_container_runner,
                                                   set_container_runner_factory)

    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: None)
    sentinel = object()
    set_container_runner_factory(lambda spec: sentinel)
    try:
        assert _make_container_runner(_spec()) is sentinel
    finally:
        set_container_runner_factory(None)


def test_a_spec_less_variation_is_untouched(monkeypatch):
    from robovast.common.config_generation import _make_container_runner

    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: None)
    assert _make_container_runner(None) is None


def test_an_actionable_refusal_keeps_its_next_step_in_a_problem():
    """A flat problem dict has no next_step field, so it must survive in the message."""
    from robovast.common.config_validation import _message_with_next_step
    from robovast.common.errors import AuxContainerUnavailable

    exc = AuxContainerUnavailable("the reason", next_step="the move")
    assert _message_with_next_step(exc) == "the reason Next: the move"
    assert _message_with_next_step(ValueError("plain")) == "plain"


def test_preview_configurations_keeps_an_actionable_refusals_next_step(monkeypatch):
    """Preview composes, so it is a place the aux-container refusal surfaces.

    It used to return ``{"error": str(e)}``, which drops the ``next_step`` riding on an
    ActionableError -- leaving the caller a reason and no move, in exactly the case where
    the move is least obvious.
    """
    from robovast.common.errors import AuxContainerUnavailable
    from robovast.mcp_server.plugins import authoring

    def _refuse(**_kwargs):
        raise AuxContainerUnavailable("needs a container", next_step="start_campaign(...)")

    monkeypatch.setattr(authoring, "_address_lane", lambda address: None)
    monkeypatch.setattr("robovast.common.common.load_config", lambda p: {})
    monkeypatch.setattr("robovast.common.config_generation.generate_scenario_variations",
                        _refuse)

    result = authoring.preview_configurations("some.vast", limit=1)
    assert result["error"] == "needs a container"
    assert result["next_step"] == "start_campaign(...)"
