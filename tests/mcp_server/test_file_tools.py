# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The five MCP file tools over the one address space.

These are thin clients of the service interface, so what is worth asserting here is
what the *tool* layer owns: that an address reaches the right root, that a failure comes
back as a message an LLM can act on rather than a traceback, and that the tools still
work with no service running — reading a local results tree has never needed one.
"""

import threading

import pytest

from robovast.mcp_server.plugins import files
from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture(name="ws")
def _ws(tmp_path, monkeypatch):
    run_dir = tmp_path / "results" / "camp-1" / "nav" / "0"
    run_dir.mkdir(parents=True)
    (run_dir / "test.xml").write_text('<testsuite tests="1"/>')
    (run_dir / "bag.mcap").write_bytes(b"\x00\x01\x02")
    exec_dir = tmp_path / "results" / "camp-1" / "_execution"
    exec_dir.mkdir(parents=True)
    (exec_dir / "outcome.json").write_text('{"status": "passed"}')

    transport = LocalTransport.__new__(LocalTransport)
    transport._campaigns = {}
    transport._lock = threading.Lock()
    transport.store = WorkspaceStore(
        registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    transport._campaigns_root = lambda: tmp_path / "results"
    monkeypatch.setattr(files, "_client", lambda: transport)
    return transport.store.registry.create("demo")["workspace_id"]


# -- reading results ---------------------------------------------------------


def test_read_a_campaign_file_by_address(ws):
    out = files.read_file("/results/camp-1/_execution/outcome.json")
    assert out["content"] == '{"status": "passed"}'
    assert out["total_lines"] == 1


def test_list_a_campaign_root_non_recursively(ws):
    out = files.list_files("/results/camp-1/")
    assert sorted(out["entries"]) == ["_execution/", "nav/"]
    assert out["total"] == 2
    # The listing is cheap by construction: names, not objects.
    assert all(isinstance(e, str) for e in out["entries"])
    assert "detailed" not in out


def test_entries_compose_with_the_echoed_address(ws):
    """A model must be able to build the next call by concatenation, not by
    re-deriving a prefix it was never told."""
    listing = files.list_files("/results/camp-1/nav/0/")
    address = listing["address"] + "test.xml"
    assert files.read_file(address)["content"].startswith("<testsuite")


# -- errors that teach -------------------------------------------------------


def test_a_binary_read_points_at_the_byte_route(ws):
    out = files.read_file("/results/camp-1/nav/0/bag.mcap")
    assert "binary" in out["error"].lower()
    assert "vast files get" in out["error"]


def test_a_malformed_address_states_the_expected_form(ws):
    out = files.read_file("nav/0/test.xml")
    assert "/<namespace>/<owner>/<path>" in out["error"]


def test_writing_to_results_says_where_writes_go(ws):
    out = files.write_file("/results/camp-1/x.vast", "a: 1")
    assert "read-only" in out["error"]
    assert "/sources/<workspace_id>/" in out["error"]


# -- writing sources ---------------------------------------------------------


def test_write_edit_read_delete_round_trip(ws):
    address = f"/sources/{ws}/demo.vast"
    assert files.write_file(address, "a: 1\n")["bytes"] == 5
    files.edit_file(address, "a: 1", "a: 2")
    assert files.read_file(address)["content"] == "a: 2"
    assert files.delete_file(address)["ok"] is True
    assert "error" in files.read_file(address)


def test_a_non_unique_edit_is_refused_with_the_count(ws):
    address = f"/sources/{ws}/demo.vast"
    files.write_file(address, "x\nx\n")
    out = files.edit_file(address, "x", "y")
    assert "not unique" in out["error"] and "2 matches" in out["error"]


def test_a_non_inline_type_is_redirected_to_the_upload_channel(ws):
    out = files.write_file(f"/sources/{ws}/run.py", "print()")
    assert "create_upload" in out["error"]


# -- the transport choice ----------------------------------------------------


def test_no_service_falls_back_to_an_explicit_local_transport(monkeypatch):
    """Reading a campaign on this host has never required a running service, and the
    fallback is constructed deliberately — not obtained by handing an empty URL to
    ``RobovastClient`` and letting it substitute one."""
    monkeypatch.setattr(
        "robovast.client.service_target.detected_service_url", lambda *a, **k: "")
    assert isinstance(files._client(), LocalTransport)


def test_a_reachable_service_is_preferred(monkeypatch):
    from robovast.service.http_client import HTTPTransport
    monkeypatch.setattr(
        "robovast.client.service_target.detected_service_url",
        lambda *a, **k: "http://127.0.0.1:8800")
    assert isinstance(files._client(), HTTPTransport)
