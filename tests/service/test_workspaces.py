# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for server-side workspaces: registry, path confinement, the
``.vast``/``.osc``-only inline rule, executable preservation, and upload tokens.
"""

import pytest

from robovast.service.workspaces import (INLINE_EXTENSIONS, WorkspaceError,
                                         WorkspaceRegistry, WorkspaceStore,
                                         _UploadTokens)


@pytest.fixture
def store(tmp_path):
    return WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))


@pytest.fixture
def ws(store):
    return store.registry.create(name="demo")["workspace_id"]


# -- registry ---------------------------------------------------------------


def test_create_list_get_delete_roundtrip(store):
    entry = store.registry.create(name="demo")
    wid = entry["workspace_id"]
    assert entry["name"] == "demo" and wid.startswith("ws-")
    assert store.registry.project_dir(wid).is_dir()
    assert [e["workspace_id"] for e in store.registry.list()] == [wid]
    assert store.registry.get(wid)["name"] == "demo"

    store.registry.delete(wid)
    assert store.registry.get(wid) is None
    assert store.registry.list() == []
    assert not (store.registry.root / wid).exists()


def test_unknown_workspace_raises(store):
    with pytest.raises(WorkspaceError):
        store.registry.require("ws-nope")
    with pytest.raises(WorkspaceError):
        store.write_file("ws-nope", "a.vast", "x")


def test_registry_survives_reload(tmp_path):
    root = tmp_path / "workspaces"
    wid = WorkspaceRegistry(root=root).create(name="persisted")["workspace_id"]
    # A fresh registry object reads the same on-disk JSON.
    assert WorkspaceRegistry(root=root).get(wid)["name"] == "persisted"


# -- inline authoring rule (.vast/.osc only) --------------------------------


@pytest.mark.parametrize("name", ["demo.vast", "scenario.osc", "nested/dir/a.vast"])
def test_inline_write_allows_vast_and_osc(store, ws, name):
    meta = store.write_file(ws, name, "content: 1\n")
    assert meta["path"] == name and meta["bytes"] > 0 and meta["sha256"]
    assert store.read_file(ws, name) == "content: 1\n"


@pytest.mark.parametrize("name", ["launch.py", "map.pgm", "notes.txt", "mesh.dae"])
def test_inline_write_rejects_other_types_and_points_at_upload(store, ws, name):
    with pytest.raises(WorkspaceError, match="create_upload"):
        store.write_file(ws, name, "x")


def test_write_returns_metadata_not_content(store, ws):
    meta = store.write_file(ws, "a.vast", "hello")
    assert set(meta) == {"path", "bytes", "sha256", "executable"}
    assert "content" not in meta  # never echo content back into the token stream


# -- edit (the token-cheap fix loop) ----------------------------------------


def test_edit_replaces_unique_string(store, ws):
    store.write_file(ws, "a.vast", "runs: 1\nname: x\n")
    store.edit_file(ws, "a.vast", "runs: 1", "runs: 5")
    assert store.read_file(ws, "a.vast") == "runs: 5\nname: x\n"


def test_edit_rejects_missing_or_ambiguous(store, ws):
    store.write_file(ws, "a.vast", "x: 1\nx: 1\n")
    with pytest.raises(WorkspaceError, match="not found"):
        store.edit_file(ws, "a.vast", "nope", "y")
    with pytest.raises(WorkspaceError, match="not unique"):
        store.edit_file(ws, "a.vast", "x: 1", "y: 2")


def test_edit_rejects_non_inline_type(store, ws):
    with pytest.raises(WorkspaceError, match="create_upload"):
        store.edit_file(ws, "run.py", "a", "b")


# -- path confinement (security-critical) -----------------------------------


@pytest.mark.parametrize("bad", [
    "../escape.vast",
    "nested/../../escape.vast",
    "/etc/passwd.vast",
    "~/secret.vast",
    "",
])
def test_path_confinement_rejects_escapes(store, ws, bad):
    with pytest.raises(WorkspaceError):
        store.write_file(ws, bad, "x")


def test_symlink_escape_is_refused(store, ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.registry.project_dir(ws) / "link").symlink_to(outside)
    # Writing "through" the symlink resolves outside the workspace → refused.
    with pytest.raises(WorkspaceError, match="escapes"):
        store.write_file(ws, "link/evil.vast", "x")


def test_confinement_allows_nested_paths_inside(store, ws):
    store.write_file(ws, "files/sub/a.vast", "x")
    assert (store.registry.project_dir(ws) / "files/sub/a.vast").is_file()


# -- upload side channel + executables --------------------------------------


def test_create_upload_then_put_writes_any_type(store, ws):
    grant = store.create_upload(ws, "files/launch.py")
    assert grant["token"] and grant["expires_in"] > 0
    meta = store.write_upload(grant["token"], b"print('hi')\n")
    assert meta["path"] == "files/launch.py"
    assert (store.registry.project_dir(ws) / "files/launch.py").read_bytes() == b"print('hi')\n"


def test_upload_token_is_one_time(store, ws):
    grant = store.create_upload(ws, "a.bin")
    store.write_upload(grant["token"], b"x")
    with pytest.raises(WorkspaceError, match="unknown or expired"):
        store.write_upload(grant["token"], b"x")


def test_upload_token_expires_after_ttl(tmp_path):
    clock = {"t": 1000.0}
    tokens = _UploadTokens(ttl_seconds=60, now_fn=lambda: clock["t"])
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "w"), tokens=tokens)
    wid = store.registry.create()["workspace_id"]
    grant = store.create_upload(wid, "a.bin")
    clock["t"] += 61  # TTL elapses before the PUT arrives
    with pytest.raises(WorkspaceError, match="unknown or expired"):
        store.write_upload(grant["token"], b"x")


def test_explicit_executable_flag_sets_bit(store, ws):
    grant = store.create_upload(ws, "postprocess.sh", executable=True)
    meta = store.write_upload(grant["token"], b"echo hi\n")
    assert meta["executable"] is True
    assert (store.registry.project_dir(ws) / "postprocess.sh").stat().st_mode & 0o111


def test_shebang_autodetect_sets_bit(store, ws):
    grant = store.create_upload(ws, "run.sh")  # no explicit flag
    meta = store.write_upload(grant["token"], b"#!/bin/bash\necho hi\n")
    assert meta["executable"] is True


def test_plain_binary_is_not_executable(store, ws):
    grant = store.create_upload(ws, "map.pgm")
    meta = store.write_upload(grant["token"], b"\x89PNG\x00binary")
    assert meta["executable"] is False


def test_upload_path_is_confined(store, ws):
    with pytest.raises(WorkspaceError):
        store.create_upload(ws, "../evil.sh")


# -- listing / delete -------------------------------------------------------


def test_list_and_delete_files(store, ws):
    store.write_file(ws, "a.vast", "x")
    store.write_upload(store.create_upload(ws, "files/b.py")["token"], b"y")
    paths = {f["path"] for f in store.list_files(ws)}
    assert paths == {"a.vast", "files/b.py"}

    store.delete_file(ws, "a.vast")
    assert {f["path"] for f in store.list_files(ws)} == {"files/b.py"}
    with pytest.raises(WorkspaceError):
        store.delete_file(ws, "a.vast")


def test_workspace_delete_does_not_touch_campaigns(store, ws, tmp_path):
    """Workspaces are independent of campaigns: deleting one leaves results alone."""
    campaign_dir = tmp_path / "results" / "demo-2026-07-16-120000"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.db").write_text("results")

    store.write_file(ws, "a.vast", "x")
    store.registry.delete(ws)

    assert (campaign_dir / "campaign.db").read_text() == "results"


def test_inline_extensions_are_vast_and_osc():
    assert set(INLINE_EXTENSIONS) == {".vast", ".osc"}
