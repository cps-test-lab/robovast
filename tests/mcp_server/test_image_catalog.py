# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``list_image_catalog``/``get_image_catalog_entry`` MCP tools.

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


#: What ``roqsim.introspection describe`` returns: the summary fields the list carries, PLUS
#: the config keys, which are the whole reason to ask.
_PLUGIN_DETAIL = {
    "name": "contact_monitor", "kind": "plugin",
    "doc": "Observation plugin: report when an entity touches something.",
    "flags": ["parallel_safe"], "package": "roqsim",
    "parameters": [
        {"name": "ignore", "example": "[floor]", "doc": "geoms excluded from the check"},
        {"name": "min_force", "example": "1.0", "doc": "contacts below this are ignored"},
    ],
}


class _DescribingClient(_FakeClient):
    """Answers ``list`` with the summary and ``describe <name>`` with the full entry."""

    def exec_in_container(self, request):
        self.exec_calls.append(request)
        if "describe" in request.command:
            name = request.command.rsplit(" ", 1)[-1]
            if name != _PLUGIN_DETAIL["name"]:
                return ExecResult(
                    exit_code=1, stdout=json.dumps({"error": f"no plugin named {name!r}"}))
            return ExecResult(exit_code=0, stdout=json.dumps(_PLUGIN_DETAIL))
        return ExecResult(exit_code=0, stdout=json.dumps(_PLUGINS_PAYLOAD))


@pytest.fixture(autouse=True)
def _clear_cache():
    """The catalog cache is module-level and process-lifetime -- reset between tests."""
    image_catalog._cache.clear()
    image_catalog._detail_cache.clear()
    yield
    image_catalog._cache.clear()
    image_catalog._detail_cache.clear()


@pytest.fixture
def service(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    return fake


# -- addressing ---------------------------------------------------------------


def test_a_non_address_is_refused_with_an_actionable_error():
    out = image_catalog.list_image_catalog(address="/home/me/x.vast")
    assert "workspace address" in out["error"]


def test_no_service_is_reported_not_worked_around(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    out = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    assert "error" in out


# -- one exec per image, not per item ------------------------------------------


def test_list_and_details_share_one_exec_for_the_same_image(service):
    listed = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    assert listed["cache"]["hit"] is False
    assert len(service.exec_calls) == 1

    details = image_catalog.get_image_catalog_entry(
        address="/sources/ws-1/a.vast", name="timeout")
    assert details["kind"] == "modifier"
    # A second question about the SAME image's catalog must not exec again.
    assert len(service.exec_calls) == 1
    assert details["cache"]["hit"] is True


def test_a_different_image_execs_again(service):
    image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    service.image = "robovast-build:different"
    image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    assert len(service.exec_calls) == 2


# -- flattening + query ---------------------------------------------------------


def test_scenario_actions_are_flattened_across_all_four_buckets(service):
    out = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    names = {item["name"] for item in out["items"]}
    assert names == {"differential_drive_robot.nav_to_pose", "timeout"}


def test_roqsim_plugins_come_from_the_flat_items_key(monkeypatch):
    fake = _FakeClient(payload=_PLUGINS_PAYLOAD)
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = image_catalog.list_image_catalog(address="/sources/ws-1/w.vast")
    assert [item["name"] for item in out["items"]] == ["contact_monitor"]


def test_query_filters_by_substring(service):
    out = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast", query="timeout")
    assert [item["name"] for item in out["items"]] == ["timeout"]


def test_query_glob_is_supported(service):
    out = image_catalog.list_image_catalog(
        address="/sources/ws-1/a.vast", query="*nav_to_pose")
    assert [item["name"] for item in out["items"]] == ["differential_drive_robot.nav_to_pose"]


# -- detail lookup --------------------------------------------------------------


def test_get_details_unknown_name_is_error(service):
    out = image_catalog.get_image_catalog_entry(address="/sources/ws-1/a.vast", name="nope")
    assert "error" in out


def test_a_roqsim_plugins_entry_keeps_its_summary_fields(monkeypatch):
    """The summary fields survive; the config keys they are asked alongside are below."""
    fake = _DescribingClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = image_catalog.get_image_catalog_entry(
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
    out = image_catalog.list_image_catalog(address="/sources/ws-1/w.vast")
    assert "error" in out
    assert "ModuleNotFoundError" in out["error"]


def test_unparseable_output_is_reported_as_error_not_raised(monkeypatch):
    class Garbled(_FakeClient):
        def exec_in_container(self, request):
            self.exec_calls.append(request)
            return ExecResult(exit_code=0, stdout="not json")

    monkeypatch.setattr(service_access, "service_client", lambda: Garbled())
    out = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    assert "error" in out


def test_one_pair_covers_both_catalogs(monkeypatch):
    """Four tools were two helpers called with a different constant.

    The surface carried four descriptions of one question — and the two that named their
    catalog in the tool name were the two callers most often got wrong, because nothing
    about the name said an ``address`` was still required.
    """
    seen = []
    monkeypatch.setattr(image_catalog, "_list",
                        lambda catalog, address, query: seen.append(catalog) or {"items": []})
    monkeypatch.setattr(image_catalog, "_details",
                        lambda catalog, address, name: seen.append(catalog) or {"name": name})

    for catalog in image_catalog.CATALOGS:
        image_catalog.list_image_catalog(address="/sources/ws-1/a.vast", catalog=catalog)
        image_catalog.get_image_catalog_entry(address="/sources/ws-1/a.vast",
                                              name="x", catalog=catalog)

    assert seen == ["scenario_actions", "scenario_actions",
                    "roqsim_plugins", "roqsim_plugins"]


def test_an_unknown_catalog_names_the_ones_that_exist():
    """A refusal that lists the alternatives is the difference between a caller fixing its
    next call and guessing again — the same contract ``list_plugins`` keeps for its groups.
    """
    out = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast", catalog="nope")

    assert "nope" in out["error"]
    for catalog in image_catalog.CATALOGS:
        assert catalog in out["error"]


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
    out = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    assert "error" not in out
    assert {i["name"] for i in out["items"]} == {
        "differential_drive_robot.nav_to_pose", "timeout"}


def test_a_roqsim_catalog_survives_the_entrypoint_banner(monkeypatch):
    monkeypatch.setattr(
        service_access, "service_client",
        lambda: _client_printing(prefix=_BANNER, payload=_PLUGINS_PAYLOAD))
    out = image_catalog.list_image_catalog(address="/sources/ws-1/w.vast", catalog="roqsim_plugins")
    assert "error" not in out
    assert [i["name"] for i in out["items"]] == ["contact_monitor"]


def test_a_line_printed_after_the_catalog_is_ignored_too(monkeypatch):
    monkeypatch.setattr(
        service_access, "service_client",
        lambda: _client_printing(prefix=_BANNER, suffix="\n[INFO] [entrypoint]: done\n"))
    out = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
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
    out = image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    assert "error" not in out
    assert out["total"] == 2


def test_output_carrying_no_json_names_the_command_that_shows_the_stream(monkeypatch):
    class Silent(_FakeClient):
        def exec_in_container(self, request):
            self.exec_calls.append(request)
            return ExecResult(exit_code=0, stdout=_BANNER)

    monkeypatch.setattr(service_access, "service_client", lambda: Silent())
    out = image_catalog.list_image_catalog(address="/sources/ws-1/w.vast", catalog="roqsim_plugins")
    assert "error" in out
    # The remedy and the evidence, not just the decoder's complaint.
    assert "exec_in_container" in out["error"]
    assert "roqsim.introspection" in out["error"]
    assert "entrypoint" in out["error"]


# -- a summary list cannot answer a detail request --------------------------------

def test_a_roqsim_plugins_detail_carries_its_config_keys(monkeypatch):
    """The tool says it reports a plugin's Config:: keys; filtering the summary list never did."""
    fake = _DescribingClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = image_catalog.get_image_catalog_entry(
        address="/sources/ws-1/w.vast", name="contact_monitor", catalog="roqsim_plugins")
    assert "error" not in out
    assert [p["name"] for p in out["parameters"]] == ["ignore", "min_force"]
    assert out["image"] == fake.image


def test_a_detail_is_asked_for_by_name_not_filtered_from_the_list(monkeypatch):
    fake = _DescribingClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    image_catalog.get_image_catalog_entry(
        address="/sources/ws-1/w.vast", name="contact_monitor", catalog="roqsim_plugins")
    assert [c.command for c in fake.exec_calls] == [
        "python3 -m roqsim.introspection describe contact_monitor"]


def test_a_second_request_for_the_same_plugin_is_cached(monkeypatch):
    fake = _DescribingClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    for _ in range(2):
        out = image_catalog.get_image_catalog_entry(
        address="/sources/ws-1/w.vast", name="contact_monitor", catalog="roqsim_plugins")
    assert len(fake.exec_calls) == 1
    assert out["cache"]["hit"] is True


def test_an_unknown_plugin_is_reported_even_though_describe_exits_nonzero(monkeypatch):
    """``describe`` says why on stdout and exits 1, so the code is not the verdict."""
    monkeypatch.setattr(service_access, "service_client", lambda: _DescribingClient())
    out = image_catalog.get_image_catalog_entry(
        address="/sources/ws-1/w.vast", name="no_such_plugin", catalog="roqsim_plugins")
    assert "no roqsim plugins entry named 'no_such_plugin'" in out["error"]


def test_a_name_that_is_not_an_entry_point_name_is_refused_before_the_container(monkeypatch):
    """The name reaches a shell in the container; anything shell-shaped is a mistake in the call."""
    fake = _DescribingClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = image_catalog.get_image_catalog_entry(
        address="/sources/ws-1/w.vast", name="x; cat /etc/passwd", catalog="roqsim_plugins")
    assert "not an entry-point name" in out["error"]
    assert fake.exec_calls == []


def test_a_scenario_action_detail_still_comes_from_the_list(monkeypatch):
    """That list already carries every field, so it must not gain a round trip."""
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    image_catalog.list_image_catalog(address="/sources/ws-1/a.vast")
    out = image_catalog.get_image_catalog_entry(address="/sources/ws-1/a.vast", name="timeout")
    assert out["kind"] == "modifier"
    assert len(fake.exec_calls) == 1
