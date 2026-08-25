# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Each check tier says what it could not settle, instead of misreporting it.

Three tools check a ``.vast`` and differ in two things: whether they install the
``plugins:`` block, and whether they compose inside an execution backend's container
context. A tier that does not report which of the two applies makes a correct file look
broken:

* a ``plugins:``-declaring ``.vast`` is told to declare the package in ``plugins:``, and
* a variation needing an auxiliary container dies on a bare ``FileNotFoundError: 'docker'``.

The invariant behind both: a name or a runner that is genuinely missing must still fail
loudly. These tests pin the difference between "cannot check that here" and "that is wrong".
"""

import contextlib
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_VAST = """\
version: 3
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
    # Blames the place, not the file. The old wording said a runner exists only inside a
    # campaign's composition and sent every caller to start_campaign — a whole real trial to
    # answer "does this expand?", and untrue besides: a preview through the service arranges
    # one too. Reading a property of *where this ran* as a property of the `.vast` is the
    # mistake this asserts against.
    assert "preview_configurations" in excinfo.value.next_step
    assert "not a defect in the file" in excinfo.value.next_step


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

    Returning ``{"error": str(e)}`` drops the ``next_step`` riding on an ActionableError --
    leaving the caller a reason and no move, in exactly the case where the move is least
    obvious.
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


# -- the container tier: preview is a place a runner is arranged ----------------------

def test_preview_composes_inside_the_lane_s_aux_runner_context(monkeypatch, tmp_path):
    """The bug this closes: preview never entered the hook that provides the runner.

    The hook existed and the cluster lane overrode it, but only ``start_campaign`` entered
    it -- so in a service pod, where there is no ``docker`` to fall back on, a sweep whose
    variation needs a helper image was refused and the refusal blamed the ``.vast``.
    """
    from robovast.service.local_transport import LocalTransport

    entered = []

    @contextlib.contextmanager
    def _record(self, tag, project, *, hold=False):
        entered.append((tag, hold))
        yield

    monkeypatch.setattr(LocalTransport, "_aux_runner_context", _record)
    monkeypatch.setattr(LocalTransport, "_resolve_project",
                        lambda self, ws, path: SimpleNamespace(config_path=str(tmp_path / "x.vast")))
    monkeypatch.setattr("robovast.common.common.load_config", lambda p: {})
    monkeypatch.setattr(
        "robovast.common.config_generation.generate_scenario_variations",
        lambda **_kw: {"configs": [], "execution": {"runs": 1},
                       "aux_containers": ["aux-builder"]})

    response = LocalTransport.preview_configurations(
        LocalTransport.__new__(LocalTransport), "ws-1", 1, "x.vast")

    (tag, hold) = entered[0]
    # Held, not span-scoped: this is the authoring loop, previewed over and over.
    assert hold is True
    # And keyed on the project, so the second preview of the same file reuses the first's.
    from robovast.service.local_transport import _preview_tag
    assert tag == _preview_tag("ws-1", "x.vast")
    # What it ran is reported, so a caller can see the answer was not free.
    assert response.aux_containers == ["aux-builder"]


def test_the_preview_tag_is_stable_per_project_and_name_safe():
    """Stable or the pod is never reused; name-safe or it cannot be a pod name at all."""
    from robovast.service.local_transport import _preview_tag

    assert _preview_tag("ws-1", "a/b.vast") == _preview_tag("ws-1", "a/b.vast")
    assert _preview_tag("ws-1", "a/b.vast") != _preview_tag("ws-2", "a/b.vast")
    assert _preview_tag("ws-1", "a/b.vast") != _preview_tag("ws-1", "a/c.vast")
    tag = _preview_tag("ws-1", "deep/nested/path with spaces.vast")
    assert re.fullmatch(r"[a-z0-9-]+", tag), tag


def test_the_base_lane_leaves_the_docker_fallback_alone():
    """A no-op locally is the whole local design: ``docker run --rm`` is already right."""
    from robovast.service.local_transport import LocalTransport

    with LocalTransport._aux_runner_context(
            LocalTransport.__new__(LocalTransport), "t", None, hold=True) as arranged:
        assert arranged is None


def test_a_campaign_still_arranges_one_after_the_split(monkeypatch):
    """``_campaign_context`` delegating is the refactor; losing the runner is the risk."""
    from robovast.service.local_transport import LocalTransport

    seen = []
    monkeypatch.setattr(
        LocalTransport, "_aux_runner_context",
        lambda self, tag, project, *, hold=False: (
            seen.append((tag, hold)) or contextlib.nullcontext()))
    with LocalTransport._campaign_context(
            LocalTransport.__new__(LocalTransport), "camp-7", None):
        pass
    # The campaign's own id, and *not* held: its span owns the container, which is what
    # lets per-campaign cleanup find it.
    assert seen == [("camp-7", False)]


def test_composition_reports_the_aux_container_it_used():
    """``aux_containers`` must cross the isolated-compose boundary and the cache untouched.

    It is a plain top-level key for exactly that reason: the underscore-prefixed fields are
    the ones ``_result_to_transport`` strips as ephemeral.
    """
    from robovast.common.config_generation import _result_from_transport, _result_to_transport

    result = {"configs": [], "aux_containers": ["aux-builder"], "_output_dir": "/tmp/x",
              "_transient_files": []}
    transport = _result_to_transport(result)
    assert transport["aux_containers"] == ["aux-builder"]
    assert _result_from_transport(transport, None)["aux_containers"] == ["aux-builder"]
