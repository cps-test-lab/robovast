# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for server-side workspaces: registry, path confinement, the
``.vast``/``.osc``-only inline rule, executable preservation, and upload tokens.
"""

import pytest

from robovast.client.file_address import SOURCES, format_address
from robovast.service.client import LocalTransport
from robovast.service.workspaces import (INLINE_EXTENSIONS, WorkspaceError, WorkspaceRegistry,
                                         WorkspaceStore, _UploadTokens)


def _listing(store, workspace_id):
    """The workspace's files as the ``/sources`` address space reports them.

    Listing is not a store method any more: it is one operation over both namespaces
    (see :mod:`robovast.client.file_address`), so it is exercised through the transport
    that resolves an address — which is also what applies this store's pinned-dir skip
    rule via :meth:`WorkspaceStore.skip_entry`.
    """
    transport = LocalTransport.__new__(LocalTransport)
    transport.store = store
    return sorted(transport.list_files(format_address(SOURCES, workspace_id),
                                       recursive=True, limit=0).entries)


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
    assert store.resolve(ws, name).read_text() == "content: 1\n"


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
    assert store.resolve(ws, "a.vast").read_text() == "runs: 5\nname: x\n"


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
    assert _listing(store, ws) == ["a.vast", "files/b.py"]

    store.delete_file(ws, "a.vast")
    assert _listing(store, ws) == ["files/b.py"]
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


# -- name uniqueness (repeated `workspace init` of the same dir) -------------


def test_duplicate_names_get_incrementing_suffix(store):
    """First keeps the bare name; repeats get -2/-3 so the dropdown stays legible."""
    names = [store.registry.create(name="ros2_basic")["name"] for _ in range(3)]
    assert names == ["ros2_basic", "ros2_basic-2", "ros2_basic-3"]


def test_suffix_avoids_pinned_name_too(tmp_path):
    """An init'd copy never shadows a pinned dir by name."""
    src = tmp_path / "ros2_basic"
    src.mkdir()
    reg = WorkspaceRegistry(root=tmp_path / "w", static_dir=str(src))
    assert reg.create(name="ros2_basic")["name"] == "ros2_basic-2"


def test_empty_name_defaults_to_id_without_suffix(store):
    entry = store.registry.create()
    assert entry["name"] == entry["workspace_id"]


# -- pinned (read-only) workspaces ------------------------------------------


@pytest.fixture
def pinned(tmp_path):
    """A store with a directory pinned read-only in place (vast serve --workspace-dir)."""
    src = tmp_path / "myproj"
    (src / "results" / "old-campaign" / "_config").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "demo.vast").write_text("configuration:\n  variations: []\n")
    (src / "run.sh").write_text("#!/bin/sh\n")
    (src / "results" / "old-campaign" / "_config" / "snap.vast").write_text("x")
    (src / ".git" / "config").write_text("x")
    registry = WorkspaceRegistry(root=tmp_path / "workspaces", static_dir=str(src))
    store = WorkspaceStore(registry=registry)
    wid = registry.list()[0]["workspace_id"]
    return store, wid, src


def test_pinned_dir_is_used_in_place_and_listed(pinned):
    store, wid, src = pinned
    assert store.registry.is_pinned(wid) is True
    assert store.registry.project_dir(wid) == src
    entry = store.registry.get(wid)
    # Pinned says *where the files live*, not that they may not be written.
    assert entry["read_only"] is False and entry["name"] == "myproj"


def test_pinned_id_is_stable_across_reload(tmp_path):
    src = tmp_path / "myproj"
    src.mkdir()
    a = WorkspaceRegistry(root=tmp_path / "w", static_dir=str(src)).list()[0]
    b = WorkspaceRegistry(root=tmp_path / "w", static_dir=str(src)).list()[0]
    assert a["workspace_id"] == b["workspace_id"]


def test_pinned_dir_is_not_persisted_to_registry(pinned):
    store, wid, _ = pinned
    # A fresh registry over the same root (no static_dir) must not see the pin.
    reloaded = WorkspaceRegistry(root=store.registry.root)
    assert reloaded.get(wid) is None


def test_pinned_listing_skips_hidden_and_results(pinned):
    store, wid, _ = pinned
    assert _listing(store, wid) == ["demo.vast", "run.sh"]


def test_pinned_dir_is_editable_in_place(pinned):
    """An edit through the service lands on the real file.

    This is what lets the web UI replace the desktop editor's Open/Save for a project that
    lives in a git working tree: without it the only route was to copy the project into the
    store, edit the copy, and copy it back.
    """
    store, wid, src = pinned
    assert "configuration" in store.resolve(wid, "demo.vast").read_text()

    store.write_file(wid, "x.vast", "version: 3\n")
    assert (src / "x.vast").read_text() == "version: 3\n"

    store.edit_file(wid, "demo.vast", "configuration", "configuration  # edited")
    assert "# edited" in (src / "demo.vast").read_text()

    store.delete_file(wid, "x.vast")
    assert not (src / "x.vast").exists()


def test_pinned_dir_cannot_be_deleted_through_service(pinned):
    # The directory is the caller's, not the store's: it is unpinned by dropping the flag.
    store, wid, src = pinned
    with pytest.raises(WorkspaceError, match="pinned in place"):
        store.registry.delete(wid)
    assert src.is_dir()


def test_pinned_dir_serves_the_cluster_lane_too(pinned):
    """A pinned dir is usable by ``--backend cluster`` running off-cluster.

    Pinning only requires the service to run on the host holding the directory --
    which an off-cluster cluster driver does, reading project inputs from this
    filesystem exactly as the local lane does. It is refused in-pod (no such
    directory) and by ``--attach`` (runs no service of its own), both enforced in
    the CLI. Without this, removing the CWD-project fallback would have forced
    cluster users through an upload for a directory sitting right there.
    """
    store, wid, src = pinned
    # ClusterService inherits _resolve_project/_project_for_workspace unchanged, so
    # exercise the resolution the cluster lane would use, with the same store.
    transport = LocalTransport(store=store)
    project = transport._resolve_project(wid, "demo.vast")
    assert project.config_path == str(src / "demo.vast")


def test_only_one_pinned_dir_is_accepted():
    """``--workspace-dir`` collapses to one directory and reports a second."""
    import click

    from robovast.common.cli.core_commands import _one_workspace_dir

    assert _one_workspace_dir(None, None, ()) is None
    assert _one_workspace_dir(None, None, ("/tmp",)) == "/tmp"
    with pytest.raises(click.BadParameter, match="takes one directory"):
        _one_workspace_dir(None, None, ("/tmp", "/var"))
