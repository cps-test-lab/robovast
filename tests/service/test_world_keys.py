# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The key check: does the campaign's pinned image know every config key its world sets?

A world can be newer than the image a campaign pins, and most plugins do not refuse a key they
do not read -- the run starts, the key does nothing, and the result looks configured. What these
tests hold:

- a key the image's plugin does not publish is ADVICE, naming the image, the plugin and the key;
- the keys roqsim injects into every component are read from the image and never reported;
- only what the world document declares is compared, and only its top-level keys;
- a check that could not be made says so, as advice -- it never passes silently and never
  takes ``valid`` away;
- the image's catalog is asked once per image, through the cache the MCP tools share.

The describe and catalog answers are fixtures in the shape the image's CLI prints them.
"""

import json

import pytest

from robovast.service import image_catalog
from robovast.service.image_catalog import INJECTED_KEYS_COMMAND
from robovast.service.interface import ExecResult, ImageResolution
from robovast.service.world_keys import unknown_key_advice

IMAGE = "ghcr.io/example/roqsim:0.1.0"

#: ``roqsim scenes describe`` of a world with a robot, a meter on it, and one manifest component.
DESCRIBE = {
    "components": [
        {"address": "robot", "ref": "spawn_robot", "origin": "document",
         "paths": ["components.robot.model", "components.robot.pose.position.x",
                   "components.robot.prefix"]},
        {"address": "robot.energy", "ref": "energy_monitor", "origin": "document",
         "paths": ["components.robot.energy.idle_w",
                   "components.robot.energy.resistive_w_per_nm2",
                   "components.robot.energy.topics.energy"]},
        {"address": "robot.diff_drive", "ref": "diff_drive", "origin": "manifest",
         "paths": ["components.robot.diff_drive.left_joints"]},
    ],
    "errors": None,
}

#: ``python3 -m roqsim.introspection describe <name>`` in an image that predates a key.
CATALOG = {
    "spawn_robot": {
        "name": "spawn_robot", "kind": "plugin",
        "parameters": [{"name": "model"}, {"name": "pose"}, {"name": "position"},
                       {"name": "x"}],
        "schema": [{"name": "model", "type": "str"}, {"name": "pose", "type": "dict"}],
        "strict_keys": True,
    },
    "energy_monitor": {
        "name": "energy_monitor", "kind": "plugin",
        "parameters": [{"name": "idle_w"}, {"name": "efficiency"}],
    },
    "diff_drive": {"name": "diff_drive", "kind": "plugin", "parameters": [{"name": "x"}]},
    "silent_plugin": {"name": "silent_plugin", "kind": "plugin", "parameters": []},
}

INJECTED = ("arm", "fault", "namespace", "prefix", "robot", "topics")


class _Image:
    """Answers the two commands the check sends, from the fixtures above."""

    def __init__(self, catalog=None, injected=INJECTED):
        self.catalog = CATALOG if catalog is None else catalog
        self.injected = injected
        self.requests = []
        self.resolved = []

    def resolve(self, request):
        self.resolved.append(request)
        return ImageResolution(image="identity-of-" + IMAGE)

    def __call__(self, request):
        self.requests.append(request)
        if request.command == INJECTED_KEYS_COMMAND:
            if self.injected is None:
                return ExecResult(exit_code=1, stderr="ImportError: cannot import name")
            return ExecResult(exit_code=0,
                              stdout=json.dumps({"injected_keys": list(self.injected)}))
        out = []
        for line in request.command.splitlines():
            if line.startswith("echo "):
                out.append(line[len("echo '"):-1])
                continue
            name = line.rsplit(" ", 1)[-1]
            entry = self.catalog.get(name, {"error": f"no roqsim.plugins entry named {name!r}"})
            out.append(json.dumps(entry))
        return ExecResult(exit_code=0, stdout="\n".join(out) + "\n")


@pytest.fixture(autouse=True)
def _clear_cache():
    for cache in (image_catalog.DETAIL_CACHE, image_catalog.INJECTED_CACHE,
                  image_catalog.LIST_CACHE):
        cache.clear()
    yield
    for cache in (image_catalog.DETAIL_CACHE, image_catalog.INJECTED_CACHE,
                  image_catalog.LIST_CACHE):
        cache.clear()


def _check(image, payload=None, config=None):
    return unknown_key_advice(
        DESCRIBE if payload is None else payload, image=IMAGE, exec_call=image,
        resolve_call=image.resolve,
        request_kwargs={"workspace_id": "ws-1", "config_path": "a.vast"}, config=config)


def test_a_key_the_images_plugin_does_not_publish_is_advice_naming_all_three():
    problems = _check(_Image())
    assert [p["severity"] for p in problems] == ["advice"]
    message = problems[0]["message"]
    assert message.startswith(f"{IMAGE}'s energy_monitor (components.robot.energy)")
    assert "'resistive_w_per_nm2'" in message
    assert "that image will ignore it" in message
    assert "idle_w" not in message, "a published key is not a finding"


def test_injected_keys_come_from_the_image_and_are_never_reported():
    """``prefix`` on the robot and ``topics`` on the meter are roqsim's own, on any component."""
    problems = _check(_Image())
    assert not any("'prefix'" in p["message"] or "'topics'" in p["message"] for p in problems)


def test_without_the_images_injected_keys_they_would_be_reported():
    """The exclusion is the image's set, not one written down here."""
    problems = _check(_Image(injected=["arm"]))
    messages = " ".join(p["message"] for p in problems)
    assert "'prefix'" in messages and "'topics'" in messages


def test_a_strict_plugin_refuses_rather_than_ignores():
    catalog = {**CATALOG, "spawn_robot": {**CATALOG["spawn_robot"], "parameters": []}}
    payload = {"components": [
        {"address": "robot", "ref": "spawn_robot", "origin": "document",
         "paths": ["components.robot.model", "components.robot.colour"]}]}
    (problem,) = _check(_Image(catalog), payload)
    assert "does not know 'colour'; that image will refuse it." in problem["message"]


def test_only_document_components_are_compared():
    """A manifest's component ships in the same image as its plugin: they cannot disagree."""
    problems = _check(_Image())
    assert not any("diff_drive" in p["message"] for p in problems)


def test_nested_keys_are_compared_at_their_top_level_only():
    """``pose.position.x`` is ``pose`` to the check; a flattened list cannot place ``x``."""
    problems = _check(_Image())
    assert not any("spawn_robot" in p["message"] for p in problems)


def test_a_plugin_that_publishes_no_keys_is_not_said_to_know_none():
    payload = {"components": [
        {"address": "s", "ref": "silent_plugin", "origin": "document",
         "paths": ["components.s.anything"]}]}
    assert _check(_Image(), payload) == []


def test_a_plugin_loaded_by_path_is_the_campaigns_own_and_is_not_asked_about():
    image = _Image()
    payload = {"components": [
        {"address": "t", "ref": "task.py:Task", "origin": "document",
         "paths": ["components.t.anything"]}]}
    assert _check(image, payload) == []
    assert image.requests == []


def test_several_keys_on_one_component_are_one_finding():
    payload = {"components": [
        {"address": "m", "ref": "energy_monitor", "origin": "document",
         "paths": ["components.m.a", "components.m.b"]}]}
    (problem,) = _check(_Image(), payload)
    assert "'a' or 'b'" in problem["message"]
    assert "that image will ignore them" in problem["message"]


def test_the_catalog_is_asked_once_per_image_in_one_exec():
    image = _Image()
    _check(image)
    assert len(image.requests) == 2, "the injected keys, then every plugin in one exec"
    assert all(r.query and r.container == "simulation" for r in image.requests)
    _check(image)
    assert len(image.requests) == 2, "a second validation of the same image is cached"


def test_an_image_that_cannot_say_its_injected_keys_is_not_checked_and_says_so():
    """An image whose roqsim predates the injected-key set cannot tell a manifest's ``prefix``
    from a key nobody reads, so a finding would be a guess."""
    (problem,) = _check(_Image(injected=None))
    assert problem["severity"] == "advice"
    assert "were not checked" in problem["message"]
    assert "INJECTED_KEYS" in problem["message"]


def test_a_plugin_the_image_does_not_have_is_named_not_skipped():
    payload = {"components": [
        {"address": "x", "ref": "newer_plugin", "origin": "document",
         "paths": ["components.x.k"]}]}
    (problem,) = _check(_Image(), payload)
    assert "has no catalog entry for newer_plugin" in problem["message"]


def test_an_image_whose_describe_has_no_paths_is_not_checked_and_says_so():
    payload = {"components": [{"address": "robot", "ref": "spawn_robot"}]}
    (problem,) = _check(_Image(), payload)
    assert "does not report each component's origin and config paths" in problem["message"]


def test_a_failing_exec_is_advice_not_an_exception():
    def _broken(_request):
        raise RuntimeError("the pool is gone")

    image = _Image()
    problems = unknown_key_advice(
        DESCRIBE, image=IMAGE, exec_call=_broken, resolve_call=image.resolve,
        request_kwargs={"workspace_id": "ws-1", "config_path": "a.vast"})
    assert [p["severity"] for p in problems] == ["advice"]
    assert "the pool is gone" in problems[0]["message"]


# -- through world_problems and the verdict ------------------------------------------------


def test_world_problems_carries_the_advice_and_the_campaign_stays_valid(tmp_path, monkeypatch):
    from robovast.common import config_generation
    from robovast.service import world_query

    image = _Image()
    monkeypatch.setattr(config_generation, "describe_world_payload",
                        lambda *a, **k: (DESCRIBE, IMAGE))
    monkeypatch.setattr(world_query, "_distinct_blocks",
                        lambda *a, **k: [(None, {"config": "w.yaml"}),
                                         ("other", {"config": "w2.yaml"})])
    problems = world_query.world_problems(
        image, resolve_call=image.resolve, workspace_id="ws-1", config_path="a.vast",
        vast_dir=str(tmp_path), parameters={})
    assert [p["severity"] for p in problems] == ["advice"], (
        "two worlds carrying one finding say it once")
    assert problems[0]["config"] is None
