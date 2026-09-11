# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``list_scenario_actions``/``get_scenario_action_details`` and
``list_roqsim_plugins``/``get_roqsim_plugin_details`` MCP tools.

The behavior these guard: an address is resolved to an image via ``resolve_image``
(no container started), the catalog command runs once via ``exec_in_container``, and a
second call against the *same* image is served from the process-local cache rather than
execing again -- the whole point of caching a container-derived answer.
"""

import json

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import image_catalog
from robovast.service.interface import ExecResult, ImageResolution

_ACTIONS_PAYLOAD = {
    "actions": [{"name": "differential_drive_robot.nav_to_pose", "kind": "action",
                "source_lib": "nav2", "doc": "Nav to a pose.", "parameters": [],
                "raw": None, "resolvable": True}],
    "modifiers": [{"name": "timeout", "kind": "modifier", "source_lib": "helpers",
                   "doc": None, "parameters": [], "raw": None, "resolvable": True}],
    "actors": [],
    "structs": [],
}

_PLUGINS_PAYLOAD = {
    "items": [{"name": "contact_monitor", "kind": "plugin",
              "doc": "Observation plugin: report when an entity touches something.",
              "flags": ["parallel_safe"], "package": "roqsim"}],
}


class _FakeClient:
    def __init__(self, image="robovast-build:abc123", payload=None):
        self.exec_calls = []
        self.resolve_calls = []
        self.image = image
        self.payload = payload if payload is not None else _ACTIONS_PAYLOAD

    def resolve_image(self, request):
        self.resolve_calls.append(request)
        return ImageResolution(image=self.image)

    def exec_in_container(self, request):
        self.exec_calls.append(request)
        return ExecResult(exit_code=0, stdout=json.dumps(self.payload))


@pytest.fixture(autouse=True)
def _clear_cache():
    """The catalog cache is module-level and process-lifetime -- reset between tests."""
    image_catalog._cache.clear()
    yield
    image_catalog._cache.clear()


@pytest.fixture
def service(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    return fake


# -- addressing ---------------------------------------------------------------


def test_a_non_address_is_refused_with_an_actionable_error():
    out = image_catalog.list_scenario_actions(address="/home/me/x.vast")
    assert "workspace address" in out["error"]


def test_no_service_is_reported_not_worked_around(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    out = image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    assert "error" in out


# -- one exec per image, not per item ------------------------------------------


def test_list_and_details_share_one_exec_for_the_same_image(service):
    listed = image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    assert listed["cache"]["hit"] is False
    assert len(service.exec_calls) == 1

    details = image_catalog.get_scenario_action_details(
        address="/sources/ws-1/a.vast", name="timeout")
    assert details["kind"] == "modifier"
    # A second question about the SAME image's catalog must not exec again.
    assert len(service.exec_calls) == 1
    assert details["cache"]["hit"] is True


def test_a_different_image_execs_again(service):
    image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    service.image = "robovast-build:different"
    image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    assert len(service.exec_calls) == 2


# -- flattening + query ---------------------------------------------------------


def test_scenario_actions_are_flattened_across_all_four_buckets(service):
    out = image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    names = {item["name"] for item in out["items"]}
    assert names == {"differential_drive_robot.nav_to_pose", "timeout"}


def test_roqsim_plugins_come_from_the_flat_items_key(monkeypatch):
    fake = _FakeClient(payload=_PLUGINS_PAYLOAD)
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = image_catalog.list_roqsim_plugins(address="/sources/ws-1/w.vast")
    assert [item["name"] for item in out["items"]] == ["contact_monitor"]


def test_query_filters_by_substring(service):
    out = image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast", query="timeout")
    assert [item["name"] for item in out["items"]] == ["timeout"]


def test_query_glob_is_supported(service):
    out = image_catalog.list_scenario_actions(
        address="/sources/ws-1/a.vast", query="*nav_to_pose")
    assert [item["name"] for item in out["items"]] == ["differential_drive_robot.nav_to_pose"]


# -- detail lookup --------------------------------------------------------------


def test_get_details_unknown_name_is_error(service):
    out = image_catalog.get_scenario_action_details(address="/sources/ws-1/a.vast", name="nope")
    assert "error" in out


def test_get_roqsim_plugin_details_full_shape(monkeypatch):
    fake = _FakeClient(payload=_PLUGINS_PAYLOAD)
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = image_catalog.get_roqsim_plugin_details(
        address="/sources/ws-1/w.vast", name="contact_monitor")
    assert out["kind"] == "plugin"
    assert out["flags"] == ["parallel_safe"]
    assert out["image"] == fake.image
    assert out["cache"]["hit"] is False


# -- failure surfacing -----------------------------------------------------------


def test_a_nonzero_exit_is_reported_as_error_not_raised(monkeypatch):
    class Failing(_FakeClient):
        def exec_in_container(self, request):
            self.exec_calls.append(request)
            return ExecResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: roqsim")

    monkeypatch.setattr(service_access, "service_client", lambda: Failing())
    out = image_catalog.list_roqsim_plugins(address="/sources/ws-1/w.vast")
    assert "error" in out
    assert "ModuleNotFoundError" in out["error"]


def test_unparseable_output_is_reported_as_error_not_raised(monkeypatch):
    class Garbled(_FakeClient):
        def exec_in_container(self, request):
            self.exec_calls.append(request)
            return ExecResult(exit_code=0, stdout="not json")

    monkeypatch.setattr(service_access, "service_client", lambda: Garbled())
    out = image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    assert "error" in out


# -- the command's output is not the only thing on the stream ---------------------

#: What an image's entrypoint announces before the command it was asked to run. The
#: leading ``[`` is why this was never reported as a banner: it opens a well-formed JSON
#: array, so a whole-stream parse dies at the second character complaining about JSON.
_BANNER = (
    "[INFO] [1789160740.258812] [entrypoint]: Running as UID: 1000, GID: 1000...\n"
    "[INFO] [1789160740.677103] [entrypoint]: sourced /opt/ros/jazzy and /ws/install\n"
)


def _client_printing(prefix="", suffix="", payload=None):
    class Noisy(_FakeClient):
        def exec_in_container(self, request):
            self.exec_calls.append(request)
            return ExecResult(
                exit_code=0, stdout=prefix + json.dumps(self.payload) + suffix)
    return Noisy(payload=payload)


def test_a_catalog_survives_the_entrypoint_banner(monkeypatch):
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _client_printing(prefix=_BANNER))
    out = image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    assert "error" not in out
    assert {i["name"] for i in out["items"]} == {
        "differential_drive_robot.nav_to_pose", "timeout"}


def test_a_roqsim_catalog_survives_the_entrypoint_banner(monkeypatch):
    monkeypatch.setattr(
        service_access, "service_client",
        lambda: _client_printing(prefix=_BANNER, payload=_PLUGINS_PAYLOAD))
    out = image_catalog.list_roqsim_plugins(address="/sources/ws-1/w.vast")
    assert "error" not in out
    assert [i["name"] for i in out["items"]] == ["contact_monitor"]


def test_a_line_printed_after_the_catalog_is_ignored_too(monkeypatch):
    monkeypatch.setattr(
        service_access, "service_client",
        lambda: _client_printing(prefix=_BANNER, suffix="\n[INFO] [entrypoint]: done\n"))
    out = image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    assert "error" not in out
    assert out["total"] == 2


def test_a_bracketed_log_line_does_not_become_the_catalog(monkeypatch):
    """A banner line that happens to BE valid JSON must not win over the document.

    ``[1, 2]`` decodes; if the scan accepted any JSON value it would return that and
    report an empty catalog, which reads as "this image has no actions" -- a wrong answer
    where an error is the honest one. Only an object is accepted, and both catalogs are one.
    """
    monkeypatch.setattr(
        service_access, "service_client",
        lambda: _client_printing(prefix="[1, 2]\n" + _BANNER))
    out = image_catalog.list_scenario_actions(address="/sources/ws-1/a.vast")
    assert "error" not in out
    assert out["total"] == 2


def test_output_carrying_no_json_names_the_command_that_shows_the_stream(monkeypatch):
    class Silent(_FakeClient):
        def exec_in_container(self, request):
            self.exec_calls.append(request)
            return ExecResult(exit_code=0, stdout=_BANNER)

    monkeypatch.setattr(service_access, "service_client", lambda: Silent())
    out = image_catalog.list_roqsim_plugins(address="/sources/ws-1/w.vast")
    assert "error" in out
    # The remedy and the evidence, not just the decoder's complaint.
    assert "exec_in_container" in out["error"]
    assert "roqsim.introspection" in out["error"]
    assert "entrypoint" in out["error"]
