# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A workspace goes to the share and comes back, over the same provider a campaign uses.

The share holds two kinds of archive under two name grammars, and the listing is the one
place that tells them apart. So the properties worth defending are the ones a second kind
could break: that a workspace is classified as a workspace and a campaign still as a
campaign, and that what comes back out is the project that went in.

Import always CREATES. An archive carries project files and no identity -- the directory
inside it names the workspace it was exported from, which means nothing on the importing
service -- so there is no id to restore, nothing to replace, and no ``force``.
"""

import tarfile

import pytest

from robovast.service.interface import CreateWorkspaceRequest
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService

_CAMPAIGN_OBJECT = "nav-2026-08-18-194018.postprocessed.tar.gz"


class _StubShare:
    """A share in a directory: uploads land as files, downloads copy them back."""

    SHARE_TYPE = "stub"

    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def list_archives_with_size(self):
        return [(p.name, p.stat().st_size) for p in sorted(self.root.iterdir())]

    def upload_archive_stream(self, fileobj, object_name, progress_callback=None):
        with open(self.root / object_name, "wb") as fh:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)

    def download_archive(self, object_name, dest_path, progress_callback=None,
                         resume_offset=0):
        with open(self.root / object_name, "rb") as src, open(dest_path, "wb") as dst:
            dst.write(src.read())

    @staticmethod
    def archive_url(object_name):
        return f"stub://{object_name}"


@pytest.fixture(name="env")
def _env(monkeypatch, tmp_path):
    """A transport whose share is a directory, and a workspace with a project in it."""
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "stub")
    transport = NullService(
        store=WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces")))
    transport._campaigns_root = lambda: tmp_path / "results"
    (tmp_path / "results").mkdir()
    share = _StubShare(tmp_path / "share")
    monkeypatch.setattr(type(transport), "_share_provider", lambda self: share)

    ws = transport.store.registry.create(name="growth sim")["workspace_id"]
    project = transport.store.registry.project_dir(ws)
    (project / "files").mkdir(parents=True)
    (project / "campaign.vast").write_text("configuration:\n  name: x\n", encoding="utf-8")
    (project / "files" / "params.yaml").write_text("a: 1\n", encoding="utf-8")
    run = project / "run.sh"
    run.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    run.chmod(0o755)
    return transport, share, ws


def test_a_workspace_round_trips_through_the_share(env):
    """Export, then import: the same files, under a new workspace."""
    transport, _share, ws = env
    archive = transport.export_workspace(ws)
    assert archive.object_name == "growth-sim.workspace.tar.gz"
    assert archive.slug == "growth-sim"

    taken = transport.create_workspace(CreateWorkspaceRequest(from_share="growth-sim"))
    assert taken.workspace_id != ws
    # Named after the archive, because the caller named nothing.
    assert taken.name == "growth-sim"

    project = transport.store.registry.project_dir(taken.workspace_id)
    assert (project / "campaign.vast").read_text(encoding="utf-8") == "configuration:\n  name: x\n"
    assert (project / "files" / "params.yaml").read_text(encoding="utf-8") == "a: 1\n"


def test_the_executable_bit_survives_the_round_trip(env):
    """It is carried out by the export, so a run script that came back non-executable would
    fail at the point of use rather than here, where nothing would explain it."""
    import os

    transport, _share, ws = env
    transport.export_workspace(ws)
    taken = transport.create_workspace(CreateWorkspaceRequest(from_share="growth-sim"))
    project = transport.store.registry.project_dir(taken.workspace_id)
    assert os.access(project / "run.sh", os.X_OK)


def test_importing_twice_makes_two_workspaces(env):
    """Nothing is replaced, so the second import is a second project -- and the registry
    suffixes the name it would otherwise duplicate."""
    transport, _share, ws = env
    transport.export_workspace(ws)
    first = transport.create_workspace(CreateWorkspaceRequest(from_share="growth-sim"))
    second = transport.create_workspace(CreateWorkspaceRequest(from_share="growth-sim"))
    assert first.workspace_id != second.workspace_id
    assert {first.name, second.name} == {"growth-sim", "growth-sim-2"}


def test_a_caller_may_name_the_imported_workspace(env):
    transport, _share, ws = env
    transport.export_workspace(ws)
    taken = transport.create_workspace(
        CreateWorkspaceRequest(name="local copy", from_share="growth-sim"))
    assert taken.name == "local copy"


def test_the_listing_tells_the_two_kinds_apart(env):
    """One listing, two grammars. A campaign archive beside a workspace one must not appear
    in the other's list: the import verbs differ, and a workspace offered as a campaign would
    be imported by a path that reads a frozen ``_config/`` out of it."""
    transport, share, ws = env
    (share.root / _CAMPAIGN_OBJECT).write_bytes(b"not really a tar")
    transport.export_workspace(ws)

    listing = transport.list_share_archives()
    assert listing.configured is True
    assert [a.campaign_id for a in listing.archives] == ["nav-2026-08-18-194018"]
    assert [w.slug for w in listing.workspaces] == ["growth-sim"]
    assert listing.workspaces[0].url == "stub://growth-sim.workspace.tar.gz"


def test_an_object_that_is_neither_is_listed_as_neither(env):
    """A share is somebody's storage and holds other things. Reporting one would offer an
    import of a file nothing here wrote."""
    transport, share, _ws = env
    (share.root / "notes.txt").write_text("hello", encoding="utf-8")
    (share.root / "backup.tar.gz").write_bytes(b"x")

    listing = transport.list_share_archives()
    assert listing.archives == []
    assert listing.workspaces == []


def test_two_sources_are_refused_rather_than_ranked(env):
    """Either answer would silently discard a source the caller asked for."""
    transport, _share, _ws = env
    with pytest.raises(ValueError, match="not both"):
        transport.create_workspace(CreateWorkspaceRequest(
            from_campaign="nav-2026-08-18-194018", from_share="growth-sim"))


def test_a_name_that_is_not_on_the_share_says_what_is(env):
    transport, _share, ws = env
    transport.export_workspace(ws)
    with pytest.raises(KeyError, match="growth-sim"):
        transport.create_workspace(CreateWorkspaceRequest(from_share="no-such-project"))


def test_an_archive_with_two_top_level_entries_is_refused(env, tmp_path):
    """Not one of ours. Guessing which directory to take would seed a project from half of
    something, and leave a workspace behind that looks authored."""
    transport, share, _ws = env
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for name in ("one", "two"):
            member = tmp_path / name
            member.write_text("x", encoding="utf-8")
            tar.add(member, arcname=f"{name}/file.txt")
    (share.root / "bad.workspace.tar.gz").write_bytes(bad.read_bytes())

    before = {w["workspace_id"] for w in transport.store.registry.list()}
    with pytest.raises(ValueError, match="top-level"):
        transport.create_workspace(CreateWorkspaceRequest(from_share="bad"))
    # No half-populated workspace is left in the picker.
    assert {w["workspace_id"] for w in transport.store.registry.list()} == before


def test_an_archive_rolled_by_hand_lands_at_the_top(env, tmp_path):
    """``tar`` writes ``./`` for the archive root. Read as the project's directory it would
    nest the whole tree one level down -- and silently, since the files all arrive."""
    transport, share, _ws = env
    rolled = tmp_path / "rolled.tar.gz"
    src = tmp_path / "src"
    src.mkdir()
    (src / "campaign.vast").write_text("configuration:\n  name: x\n", encoding="utf-8")
    with tarfile.open(rolled, "w:gz") as tar:
        tar.add(src, arcname="./ws-elsewhere")
    (share.root / "rolled.workspace.tar.gz").write_bytes(rolled.read_bytes())

    taken = transport.create_workspace(CreateWorkspaceRequest(from_share="rolled"))
    project = transport.store.registry.project_dir(taken.workspace_id)
    assert (project / "campaign.vast").exists()
    assert not (project / "ws-elsewhere").exists()


def test_a_member_that_would_escape_the_workspace_is_refused(env, tmp_path):
    """An archive is the one input here that came from another machine."""
    transport, share, _ws = env
    evil = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("x", encoding="utf-8")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="ws-x/../../escaped.txt")
    (share.root / "evil.workspace.tar.gz").write_bytes(evil.read_bytes())

    with pytest.raises(ValueError, match="outside"):
        transport.create_workspace(CreateWorkspaceRequest(from_share="evil"))
    assert not (tmp_path / "escaped.txt").exists()


def test_exporting_without_a_share_is_a_refusal_not_a_silence(monkeypatch, tmp_path):
    """A service with no share cannot publish, and saying so is the only useful answer."""
    monkeypatch.delenv("ROBOVAST_SHARE_TYPE", raising=False)
    transport = NullService(
        store=WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces")))
    ws = transport.store.registry.create(name="lonely")["workspace_id"]
    with pytest.raises(RuntimeError, match="no share configured"):
        transport.export_workspace(ws)
