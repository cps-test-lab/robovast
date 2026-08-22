# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared directory-sync glue in ``robovast.service.project_push``:
``sync_directory_to_workspace`` (additive + ``prune``), workspace id-or-name
resolution, that ``push_project_to_workspace`` still behaves after being
refactored onto the shared ``_upload_one`` dispatch, and the launch path
(``workspace_for_project`` reuse + ``run_project_via_service``).

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
from robovast.service.project_push import (_resolve_workspace_id, push_file, push_project_files,
                                           push_project_to_workspace, run_project_via_service,
                                           sync_directory_to_workspace, workspace_for_project)
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
    default cannot catch: `vast results download` and `exec cluster run
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


# -- regression: push_project_to_workspace after the refactor ----------------


def test_push_project_creates_workspace_and_uploads_inputs(client, project):
    wid = push_project_to_workspace(client, str(project / "demo.vast"))
    paths = _paths(client, wid)
    assert "demo.vast" in paths and "scenes/room.json" in paths and "run.sh" in paths
    assert not any(p.startswith(".") for p in paths)
    # A campaign's own output is not project input: pushing results/ uploads every past
    # campaign on disk, on every launch. Same exclusion `vast workspace init` makes.
    assert not any(p.startswith("results/") for p in paths)


# -- the launch path: one workspace per project, not per launch --------------


def _names(client):
    return sorted(w.name for w in client.list_workspaces().workspaces)


def _live_campaign(campaign_id, workspace_id, done=False):
    """A campaign entry as the service holds one while it drives the run."""
    from robovast.client.status import Phase
    from robovast.execution.control_server import ControllerState
    from robovast.service.local_transport import _LocalCampaign

    state = ControllerState(campaign_id=campaign_id)
    state.set_phase(Phase.FINISHED if done else Phase.RUNNING)
    return _LocalCampaign(campaign_id, "results", state, workspace_id=workspace_id)


def test_workspace_for_project_creates_then_reuses(client, project):
    vast = str(project / "demo.vast")
    first, action = workspace_for_project(client, vast)
    assert action == "created"
    second, action = workspace_for_project(client, vast)
    assert (second, action) == (first, "reused")
    # The whole point: a second launch must not leave 'myproj-2' behind.
    assert _names(client) == ["myproj"]


def test_workspace_for_project_honours_explicit_name(client, project):
    wid, action = workspace_for_project(client, str(project / "demo.vast"), "other")
    assert action == "created"
    assert _names(client) == ["other"]
    assert client.get_workspace(wid).name == "other"


def test_workspace_for_project_asks_before_reusing(client, project):
    vast = str(project / "demo.vast")
    workspace_for_project(client, vast)

    asked = []
    wid, action = workspace_for_project(
        client, vast, on_exists=lambda name, wid_: asked.append((name, wid_)) or True)
    assert action == "reused"
    assert asked == [("myproj", wid)]


def test_workspace_for_project_declined_refuses(client, project):
    vast = str(project / "demo.vast")
    workspace_for_project(client, vast)
    with pytest.raises(ValueError, match="declined to overwrite"):
        workspace_for_project(client, vast, on_exists=lambda name, wid: False)


def test_workspace_for_project_refuses_one_a_campaign_is_reading(client, project):
    # A campaign reads its project out of the workspace for its whole life, so a push
    # now would change an experiment that is still running. Not the caller's to accept:
    # this refuses before the overwrite prompt is even reached.
    vast = str(project / "demo.vast")
    wid, _ = workspace_for_project(client, vast)
    client._campaigns["camp-live"] = _live_campaign("camp-live", wid)

    with pytest.raises(ValueError, match="camp-live"):
        workspace_for_project(client, vast, on_exists=lambda name, w: True)


def test_a_finished_campaign_does_not_hold_its_workspace(client, project):
    vast = str(project / "demo.vast")
    wid, _ = workspace_for_project(client, vast)
    client._campaigns["camp-done"] = _live_campaign("camp-done", wid, done=True)

    assert workspace_for_project(client, vast) == (wid, "reused")


def test_list_workspaces_names_the_campaigns_reading_each(client, project):
    wid, _ = workspace_for_project(client, str(project / "demo.vast"))
    other = _wid(client, "unrelated")
    client._campaigns["camp-live"] = _live_campaign("camp-live", wid)

    by_id = {w.workspace_id: w for w in client.list_workspaces().workspaces}
    assert by_id[wid].running_campaigns == ["camp-live"]
    assert by_id[other].running_campaigns == []


def test_workspace_for_project_refuses_duplicate_names():
    # The registry auto-suffixes, so same-named rows are hand-made — never guess.
    stub = _StubClient(("ws-1", "dup"), ("ws-2", "dup"))
    with pytest.raises(ValueError, match="2 workspaces are named"):
        workspace_for_project(stub, "/tmp/dup/demo.vast")


def test_push_project_files_prunes_what_the_project_no_longer_has(client, project):
    vast = project / "demo.vast"
    wid = _wid(client)
    push_project_files(client, wid, str(vast))
    (project / "scenes" / "room.json").unlink()

    stats = push_project_files(client, wid, str(vast), prune=True)
    assert "scenes/room.json" not in _paths(client, wid)
    assert stats["pruned"] == 1


def test_push_project_files_prunes_a_renamed_vast(client, project):
    # A second .vast makes the workspace unlaunchable, so prune must reach one that
    # _is_project_input skips for being the wrong name.
    wid = _wid(client)
    push_project_files(client, wid, str(project / "demo.vast"))
    (project / "demo.vast").rename(project / "renamed.vast")

    push_project_files(client, wid, str(project / "renamed.vast"), prune=True)
    assert [p for p in _paths(client, wid) if p.endswith(".vast")] == ["renamed.vast"]


def test_push_project_files_leaves_the_services_own_cache_alone(client, project):
    # A campaign writes .cache/ (config generation) into the project dir it runs from.
    # Pruning it forces a full regeneration on every relaunch — and can delete it while
    # a campaign is still reading it.
    wid = _wid(client)
    push_project_files(client, wid, str(project / "demo.vast"))
    # As the service would: a file under .cache/, which no push ever writes.
    cache_src = project / "cache-payload.json"
    cache_src.write_text("{}")
    push_file(client, _address(wid, ".cache/config_generation_abc.json"), cache_src)
    cache_src.unlink()

    stats = push_project_files(client, wid, str(project / "demo.vast"), prune=True)
    assert ".cache/config_generation_abc.json" in _paths(client, wid)
    assert stats["pruned"] == 0


# -- run_project_via_service: what reaches CreateCampaignRequest -------------


class _LaunchClient:
    """A push-capable client that records the campaign request instead of running it."""

    def __init__(self, inner):
        self._inner = inner
        self.request = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create_campaign(self, request):
        self.request = request
        return SimpleNamespace(campaign_id="camp-1", note=None)


def test_run_project_leaves_runs_to_the_vast_by_default(client, project):
    launcher = _LaunchClient(client)
    run_project_via_service(launcher, str(project / "demo.vast"), feedback=lambda _: None)
    # 0, not 1: the service reads a non-positive count as "use execution.runs", and a
    # stand-in for "unset" shrinks the campaign without failing anything.
    assert launcher.request.runs == 0


def test_run_project_forwards_description_and_reuses_the_workspace(client, project):
    vast = str(project / "demo.vast")
    launcher = _LaunchClient(client)
    run_project_via_service(launcher, vast, description="pilot: 5 reps",
                            feedback=lambda _: None)
    first = launcher.request.workspace_id
    assert launcher.request.description == "pilot: 5 reps"

    run_project_via_service(launcher, vast, feedback=lambda _: None)
    assert launcher.request.workspace_id == first
    assert _names(client) == ["myproj"]


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
    (source / "campaign.vast").write_text("version: 2\n", encoding="utf-8")
    (source / "scenario.osc").write_text("# scenario\n", encoding="utf-8")
    (source / "files" / "params.yaml").write_text("a: 1\n", encoding="utf-8")

    client = _transport(tmp_path / "store")
    workspace = client.create_workspace(CreateWorkspaceRequest(name="round-trip"))
    sync_directory_to_workspace(client, workspace.workspace_id, source)

    target = tmp_path / "pulled"
    counts = pull_workspace_to_directory(client, workspace.workspace_id, target)

    assert counts["fetched"] == 3
    assert (target / "campaign.vast").read_text(encoding="utf-8") == "version: 2\n"
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
    (source / "campaign.vast").write_text("version: 2\n", encoding="utf-8")

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
    assert (target / "campaign.vast").read_text(encoding="utf-8") == "version: 2\n"


def test_the_executable_bit_survives_the_round_trip(tmp_path):
    """It is carried *out* by push_file, so a run script that came back non-executable would fail
    at the point of use rather than here -- where nothing would explain it."""
    import os

    from robovast.service.project_push import pull_workspace_to_directory

    source = tmp_path / "project"
    source.mkdir()
    (source / "campaign.vast").write_text("version: 2\n", encoding="utf-8")
    script = source / "run.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    script.chmod(0o755)

    client = _transport(tmp_path / "store")
    workspace = client.create_workspace(CreateWorkspaceRequest(name="exec-bit"))
    sync_directory_to_workspace(client, workspace.workspace_id, source)

    target = tmp_path / "pulled"
    pull_workspace_to_directory(client, workspace.workspace_id, target)
    assert os.access(target / "run.sh", os.X_OK)
