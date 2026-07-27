# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared directory-sync glue in ``robovast.service.project_push``:
``sync_directory_to_workspace`` (additive + ``prune``), workspace id-or-name
resolution, and that ``push_project_to_workspace`` still behaves after being
refactored onto the shared ``_upload_one`` dispatch.

Everything runs against a real ``WorkspaceStore`` behind an in-process
``LocalTransport`` — the same client path the CLI and MCP tool take locally, so
``grant.url`` is ``None`` and uploads go through ``store.write_upload``.
"""

from types import SimpleNamespace

import pytest

from robovast.service.client import LocalTransport
from robovast.service.interface import CreateWorkspaceRequest
from robovast.service.project_push import (_resolve_workspace_id,
                                           push_project_to_workspace,
                                           sync_directory_to_workspace)
from robovast.service.workspaces import WorkspaceError, WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def client(tmp_path):
    lt = LocalTransport.__new__(LocalTransport)
    lt.store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return lt


@pytest.fixture
def project(tmp_path):
    """A local project dir with a .vast, a nested run file, and things to skip."""
    root = tmp_path / "myproj"
    (root / "scenes").mkdir(parents=True)
    (root / "results" / "old").mkdir(parents=True)
    (root / "demo.vast").write_text("configuration:\n  variations: []\n")
    (root / "scenes" / "room.json").write_text('{"walls": 4}')
    (root / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (root / ".hidden").write_text("secret")
    (root / "results" / "old" / "campaign.db").write_text("stale")
    return root


def _paths(client, wid):
    return sorted(f.path for f in client.list_project_files(wid).files)


def _wid(client, name="demo"):
    return client.create_workspace(CreateWorkspaceRequest(name=name)).workspace_id


# -- sync: additive upload --------------------------------------------------


def test_sync_uploads_inline_and_side_channel_skipping_hidden_and_results(client, project):
    wid = _wid(client)
    stats = sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})

    assert _paths(client, wid) == ["demo.vast", "run.sh", "scenes/room.json"]
    assert stats == {"written": 1, "uploaded": 2, "pruned": 0}  # .vast inline; run.sh + json uploaded
    # .vast content written inline, nested path preserved.
    assert client.read_project_file(wid, "demo.vast").content.startswith("configuration:")


def test_sync_preserves_executable_bit(client, project):
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    meta = {f.path: f for f in client.list_project_files(wid).files}
    assert meta["run.sh"].executable is True     # shebang / +x preserved
    assert meta["scenes/room.json"].executable is False


def test_sync_overwrites_in_place(client, project):
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    (project / "demo.vast").write_text("configuration:\n  variations: [changed]\n")
    (project / "scenes" / "room.json").write_text('{"walls": 8}')

    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    assert _paths(client, wid) == ["demo.vast", "run.sh", "scenes/room.json"]  # no duplicates
    assert "changed" in client.read_project_file(wid, "demo.vast").content


def test_sync_echo_reports_each_change(client, project):
    wid = _wid(client)
    lines = []
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"}, echo=lines.append)
    assert any("+ demo.vast" in ln for ln in lines)
    assert all(ln.strip().startswith("+") for ln in lines)  # no prune lines without --prune


# -- sync: prune (full mirror) ----------------------------------------------


def test_sync_additive_leaves_server_only_files(client, project):
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    (project / "demo.vast").unlink()  # gone locally

    stats = sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    assert "demo.vast" in _paths(client, wid)   # additive never deletes
    assert stats["pruned"] == 0


def test_sync_prune_removes_server_only_files(client, project):
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    (project / "demo.vast").unlink()

    lines = []
    stats = sync_directory_to_workspace(
        client, wid, project, skip_dirs={"results"}, prune=True, echo=lines.append)
    assert "demo.vast" not in _paths(client, wid)
    assert stats["pruned"] == 1
    assert any("- demo.vast (pruned)" in ln for ln in lines)


def test_prune_keeps_files_still_present_locally(client, project):
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    stats = sync_directory_to_workspace(
        client, wid, project, skip_dirs={"results"}, prune=True)
    assert _paths(client, wid) == ["demo.vast", "run.sh", "scenes/room.json"]
    assert stats["pruned"] == 0


# -- id-or-name resolution --------------------------------------------------
#
# The resolver only calls ``client.list_workspaces()``, so a tiny stub exercises
# its branches without depending on the registry's name auto-suffixing.


class _StubClient:
    def __init__(self, *entries):
        self._entries = entries  # (workspace_id, name) pairs

    def list_workspaces(self):
        return SimpleNamespace(workspaces=[
            SimpleNamespace(workspace_id=w, name=n) for w, n in self._entries])


def test_resolve_by_id_is_passthrough():
    # A ws-… id is trusted as-is; no lookup needed.
    assert _resolve_workspace_id(_StubClient(), "ws-abc123") == "ws-abc123"


def test_resolve_by_unique_name():
    stub = _StubClient(("ws-1", "alpha"), ("ws-2", "beta"))
    assert _resolve_workspace_id(stub, "alpha") == "ws-1"


def test_resolve_missing_name_fails_loudly():
    with pytest.raises(ValueError, match="no workspace named"):
        _resolve_workspace_id(_StubClient(("ws-1", "alpha")), "nope")


def test_resolve_ambiguous_name_fails_loudly():
    stub = _StubClient(("ws-1", "dup"), ("ws-2", "dup"))
    with pytest.raises(ValueError, match="ambiguous"):
        _resolve_workspace_id(stub, "dup")


def test_sync_refuses_read_only_pinned_workspace(tmp_path, project):
    registry = WorkspaceRegistry(root=tmp_path / "w", static_dir=str(project))
    lt = LocalTransport.__new__(LocalTransport)
    lt.store = WorkspaceStore(registry=registry)
    wid = registry.list()[0]["workspace_id"]
    with pytest.raises(WorkspaceError, match="read-only"):
        sync_directory_to_workspace(lt, wid, project, skip_dirs={"results"})


# -- regression: push_project_to_workspace after the refactor ----------------


def test_push_project_creates_workspace_and_uploads_inputs(client, project):
    wid = push_project_to_workspace(client, str(project / "demo.vast"))
    paths = _paths(client, wid)
    assert "demo.vast" in paths and "scenes/room.json" in paths and "run.sh" in paths
    # push's _is_project_input drops hidden files (it does not special-case results/,
    # which a fresh project never contains) — unchanged by the _upload_one refactor.
    assert not any(p.startswith(".") for p in paths)
