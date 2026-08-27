# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared directory-sync glue in ``robovast.service.project_push``:
``sync_directory_to_workspace`` (additive + ``prune``), workspace id-or-name
resolution, the refusal to sync into a workspace a campaign is reading, and the
round trip back out to a directory.

Nothing here launches a campaign: that is ``create_campaign`` from a workspace id and a
path, driven by ``vast workspace run`` and covered in
``tests/execution/test_cli_workspace_run_launch.py``.

Everything runs against a real ``WorkspaceStore`` behind an in-process
``LocalTransport`` — the same client path the CLI and MCP tool take locally, so
``grant.url`` is ``None`` and uploads go through ``store.write_upload``.
"""

import threading
from types import SimpleNamespace

import pytest

from robovast.client.file_address import SOURCES, format_address
from robovast.service.client import LocalTransport
from robovast.service.interface import CreateWorkspaceRequest
from robovast.service.project_push import (_resolve_workspace_id,
                                           sync_directory_to_workspace)
from robovast.service.workspaces import WorkspaceError, WorkspaceRegistry, WorkspaceStore


def _transport(root):
    lt = LocalTransport.__new__(LocalTransport)
    lt.store = WorkspaceStore(registry=WorkspaceRegistry(root=root))
    # ``list_workspaces`` reports which workspaces live campaigns are reading from, so
    # the campaign registry has to exist even for a store-only transport.
    lt._campaigns = {}
    lt._lock = threading.Lock()
    return lt


@pytest.fixture
def client(tmp_path):
    return _transport(tmp_path / "workspaces")


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


def _address(wid, rel=""):
    return format_address(SOURCES, wid, rel)


def _paths(client, wid):
    return sorted(client.list_files(_address(wid), recursive=True, limit=0).entries)


def _meta(client, wid):
    listing = client.list_files(_address(wid), recursive=True, detail=True, limit=0)
    return {e.name: e for e in listing.detailed}


def _content(client, wid, rel):
    return client.read_file(_address(wid, rel)).content


def _wid(client, name="demo"):
    return client.create_workspace(CreateWorkspaceRequest(name=name)).workspace_id


def _live_campaign(campaign_id, workspace_id, done=False):
    """A campaign entry as the service holds one while it drives the run."""
    from robovast.client.status import Phase
    from robovast.execution.control_server import ControllerState
    from robovast.service.local_transport import _LocalCampaign

    state = ControllerState(campaign_id=campaign_id)
    state.set_phase(Phase.FINISHED if done else Phase.RUNNING)
    return _LocalCampaign(campaign_id, "results", state, workspace_id=workspace_id)


# -- sync: refused while a campaign is reading the workspace ----------------


def test_sync_refuses_a_workspace_a_campaign_is_reading(client, project):
    # A campaign reads its project out of the workspace for its whole life, so a sync now
    # would change an experiment that is still running. Not the caller's to accept, so it
    # is a refusal and not a prompt.
    wid = _wid(client)
    client._campaigns["camp-live"] = _live_campaign("camp-live", wid)

    with pytest.raises(ValueError, match="camp-live"):
        sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})


def test_sync_allows_a_workspace_whose_campaign_has_finished(client, project):
    wid = _wid(client)
    client._campaigns["camp-done"] = _live_campaign("camp-done", wid, done=True)

    stats = sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    assert stats["written"] + stats["uploaded"] == 3


# -- sync: additive upload --------------------------------------------------


def test_sync_uploads_inline_and_side_channel_skipping_hidden_and_results(client, project):
    wid = _wid(client)
    stats = sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})

    assert _paths(client, wid) == ["demo.vast", "run.sh", "scenes/room.json"]
    # .vast inline; run.sh + json uploaded. `skipped_dirs` counts results/, which is
    # reported rather than dropped in silence.
    assert stats == {"written": 1, "uploaded": 2, "pruned": 0, "skipped_dirs": 1}
    # .vast content written inline, nested path preserved.
    assert _content(client, wid, "demo.vast").startswith("configuration:")


def test_sync_preserves_executable_bit(client, project):
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    meta = _meta(client, wid)
    assert meta["run.sh"].executable is True     # shebang / +x preserved
    assert meta["scenes/room.json"].executable is False


def test_sync_overwrites_in_place(client, project):
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    (project / "demo.vast").write_text("configuration:\n  variations: [changed]\n")
    (project / "scenes" / "room.json").write_text('{"walls": 8}')

    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    assert _paths(client, wid) == ["demo.vast", "run.sh", "scenes/room.json"]  # no duplicates
    assert "changed" in _content(client, wid, "demo.vast")


def test_sync_echo_reports_each_change(client, project):
    wid = _wid(client)
    lines = []
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"}, echo=lines.append)
    assert any("+ demo.vast" in ln for ln in lines)
    # No prune lines without --prune. The skip report is the other legitimate output, so
    # this checks for the "-" a prune would emit rather than for "everything is a +".
    assert not any(ln.strip().startswith("-") for ln in lines)


# -- sync: campaign results are not project input ---------------------------


def _campaign_dir(root, name):
    """A directory that looks like campaign output because it CONTAINS the markers.

    Named after a campaign id rather than "results", which is the case the name-based
    default cannot catch: `vast campaign download` and `exec cluster run
    --wait-and-download` both land a campaign under its own id.
    """
    d = root / name
    (d / "_execution").mkdir(parents=True)
    (d / "metadata.yaml").write_text("campaign_id: x\n")
    (d / "_execution" / "outcome.json").write_text("{}")
    (d / "cfg-1" / "0").mkdir(parents=True)
    (d / "cfg-1" / "0" / "poses.csv").write_text("frame,x\n")
    return d


def test_sync_skips_a_results_tree_named_after_its_campaign(client, project):
    _campaign_dir(project, "demo-2026-08-21-09291829")
    wid = _wid(client)
    stats = sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})

    # The campaign's files are absent even though nothing named it: recognised by content.
    assert _paths(client, wid) == ["demo.vast", "run.sh", "scenes/room.json"]
    assert stats["skipped_dirs"] == 2       # results/ by name, the campaign by content


def test_sync_reports_what_it_skipped_and_how_to_include_it(client, project):
    _campaign_dir(project, "demo-2026-08-21-09291829")
    wid = _wid(client)
    lines = []
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"},
                                echo=lines.append)
    report = "\n".join(lines)
    assert "demo-2026-08-21-09291829" in report      # named, not silently dropped
    assert "campaign results" in report              # why
    assert "--include-results" in report             # how to override


def test_include_results_uploads_them_anyway(client, project):
    _campaign_dir(project, "demo-2026-08-21-09291829")
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"},
                                include_results=True)
    assert "demo-2026-08-21-09291829/cfg-1/0/poses.csv" in _paths(client, wid)
    # `results` is excluded BY NAME, so the content override must not resurrect it --
    # the two filters are independent and --include-results only relaxes the content one.
    assert not any(p.startswith("results/") for p in _paths(client, wid))


def test_a_project_with_only_one_marker_is_not_mistaken_for_results(client, project):
    """One marker is not enough: a project may legitimately own a metadata.yaml."""
    (project / "docs").mkdir()
    (project / "docs" / "metadata.yaml").write_text("title: notes\n")
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    assert "docs/metadata.yaml" in _paths(client, wid)


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


def test_a_directory_that_is_not_there_is_refused(client, project, tmp_path):
    """``rglob`` yields nothing for a missing path rather than raising, so the sync
    reported a contented ``{"written": 0, "uploaded": 0}`` for a push that pushed
    nothing at all."""
    wid = _wid(client)
    with pytest.raises(FileNotFoundError, match="no such directory"):
        sync_directory_to_workspace(client, wid, tmp_path / "typo")


def test_prune_against_a_missing_directory_deletes_nothing(client, project, tmp_path):
    """The reason the check above is worth having.

    "No local files" and "the path is wrong" were the same state, so prune concluded
    the workspace should be empty and deleted every file in it. A mistyped path was
    enough -- and against a remote service the directory is read on the service host,
    where a path from the caller's machine is *expected* to be absent.
    """
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    before = _paths(client, wid)
    assert before, "fixture must have uploaded something for this to mean anything"

    with pytest.raises(FileNotFoundError):
        sync_directory_to_workspace(client, wid, tmp_path / "typo", prune=True)

    assert _paths(client, wid) == before


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


def test_sync_refuses_a_pinned_workspace(tmp_path, project):
    """A pinned directory takes individual edits but is never mirrored wholesale.

    The edits are the point of pinning; a whole-tree sync overwrites every file and, with
    --prune, deletes the ones the source lacks -- against a directory the caller owns, and
    with a plain alternative (edit it on disk).
    """
    registry = WorkspaceRegistry(root=tmp_path / "w", static_dir=str(project))
    lt = LocalTransport.__new__(LocalTransport)
    lt.store = WorkspaceStore(registry=registry)
    wid = registry.list()[0]["workspace_id"]
    with pytest.raises(WorkspaceError, match="pinned in place"):
        sync_directory_to_workspace(lt, wid, project, skip_dirs={"results"})


# -- list_workspaces names the campaigns reading each workspace --------------


def test_list_workspaces_names_the_campaigns_reading_each(client, project):
    wid = _wid(client)
    sync_directory_to_workspace(client, wid, project, skip_dirs={"results"})
    other = _wid(client, "unrelated")
    client._campaigns["camp-live"] = _live_campaign("camp-live", wid)

    by_id = {w.workspace_id: w for w in client.list_workspaces().workspaces}
    assert by_id[wid].running_campaigns == ["camp-live"]
    assert by_id[other].running_campaigns == []


# ---------------------------------------------------------------------------
# Pulling a workspace back out
# ---------------------------------------------------------------------------

def test_a_workspace_round_trips_through_a_directory(tmp_path):
    """Push a project, pull it back, and get the same files.

    The other direction of ``sync_directory_to_workspace``, so a project can be taken off a remote
    service and worked on locally. Built on the existing per-file calls rather than a new archive
    route: a workspace is a source project, where that is adequate -- a campaign is the case that
    needs an archive, because it holds rosbags.
    """
    from robovast.service.project_push import pull_workspace_to_directory

    source = tmp_path / "project"
    (source / "files").mkdir(parents=True)
    (source / "campaign.vast").write_text("version: 3\n", encoding="utf-8")
    (source / "scenario.osc").write_text("# scenario\n", encoding="utf-8")
    (source / "files" / "params.yaml").write_text("a: 1\n", encoding="utf-8")

    client = _transport(tmp_path / "store")
    workspace = client.create_workspace(CreateWorkspaceRequest(name="round-trip"))
    sync_directory_to_workspace(client, workspace.workspace_id, source)

    target = tmp_path / "pulled"
    counts = pull_workspace_to_directory(client, workspace.workspace_id, target)

    assert counts["fetched"] == 3
    assert (target / "campaign.vast").read_text(encoding="utf-8") == "version: 3\n"
    assert (target / "scenario.osc").read_text(encoding="utf-8") == "# scenario\n"
    # Nested paths survive, rather than being flattened into the target root.
    assert (target / "files" / "params.yaml").read_text(encoding="utf-8") == "a: 1\n"


def test_pulling_refuses_to_overwrite_local_files(tmp_path):
    """Pulling into a directory holding an edited copy of the same project is the likely mistake,
    and overwriting somebody's local edits is not recoverable -- so it is refused per file rather
    than checked once for the directory."""
    from robovast.service.project_push import pull_workspace_to_directory

    source = tmp_path / "project"
    source.mkdir()
    (source / "campaign.vast").write_text("version: 3\n", encoding="utf-8")

    client = _transport(tmp_path / "store")
    workspace = client.create_workspace(CreateWorkspaceRequest(name="no-clobber"))
    sync_directory_to_workspace(client, workspace.workspace_id, source)

    target = tmp_path / "pulled"
    target.mkdir()
    (target / "campaign.vast").write_text("my local edits\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        pull_workspace_to_directory(client, workspace.workspace_id, target)
    assert (target / "campaign.vast").read_text(encoding="utf-8") == "my local edits\n"

    pull_workspace_to_directory(client, workspace.workspace_id, target, overwrite=True)
    assert (target / "campaign.vast").read_text(encoding="utf-8") == "version: 3\n"


def test_the_executable_bit_survives_the_round_trip(tmp_path):
    """It is carried *out* by push_file, so a run script that came back non-executable would fail
    at the point of use rather than here -- where nothing would explain it."""
    import os

    from robovast.service.project_push import pull_workspace_to_directory

    source = tmp_path / "project"
    source.mkdir()
    (source / "campaign.vast").write_text("version: 3\n", encoding="utf-8")
    script = source / "run.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    script.chmod(0o755)

    client = _transport(tmp_path / "store")
    workspace = client.create_workspace(CreateWorkspaceRequest(name="exec-bit"))
    sync_directory_to_workspace(client, workspace.workspace_id, source)

    target = tmp_path / "pulled"
    pull_workspace_to_directory(client, workspace.workspace_id, target)
    assert os.access(target / "run.sh", os.X_OK)
