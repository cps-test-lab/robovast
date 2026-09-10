# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for what a held exec container's identity has to carry about a workspace.

The failure these guard against is a *stale answer*: a query container is held so the
next call is warm, and the project reaches it exactly once, when the container is created
-- the cluster lane mirrors it in with an init container, the local lane bind-mounts it.
A campaign may be identified by its id alone because it is frozen once it starts, but a
workspace is editable by definition, so its id names a tree whose contents change under
it. Reuse keyed on the id alone answers an edited workspace from the bytes it no longer
holds, which is a `validate_project` that keeps reporting the very problem its own fix
removed -- and reports it as the world's, not as its own.
"""

import os
import time

import pytest

from robovast.service.client import LocalTransport
from robovast.service.container_exec import ExecSpec
from robovast.service.image_store import ImageRef
from robovast.service.interface import ExecRequest
from robovast.service.local_transport import _workspace_sha
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def local(tmp_path):
    return LocalTransport(
        store=WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces")))


def _spec(**kw):
    return ExecSpec(image="img", command="true", config_dir="/tmp/cfg", env={}, **kw)


# -- the fingerprint itself --------------------------------------------------


#: Comfortably past a filesystem timestamp tick, measured at 1 ms on ext4. A test that
#: rewrites a file to the same length has only the inode timestamps to tell the two
#: versions apart, so without this it passes or fails on which side of the tick it lands.
_PAST_A_TICK_S = 0.01


def _at(tmp_path):
    spec = _spec()
    spec.workspace_dir = str(tmp_path)
    return spec


def test_a_changed_file_is_a_changed_tree(tmp_path):
    (tmp_path / "world.yaml").write_text("pos: [40, 40]\n")
    before = _workspace_sha(_at(tmp_path))
    (tmp_path / "world.yaml").write_text("pose: {position: {x: 40, y: 40}}\n")
    assert _workspace_sha(_at(tmp_path)) != before


def test_a_rewrite_is_a_changed_tree_even_at_the_same_size(tmp_path):
    # Size alone would miss this; the inode timestamps are what catch it. The fingerprint
    # is stat-based, so this is the case with the least margin -- if a filesystem's
    # timestamp granularity were too coarse to separate two writes, it would show here.
    world = tmp_path / "world.yaml"
    world.write_text("a: 1\n")
    before = _workspace_sha(_at(tmp_path))
    time.sleep(_PAST_A_TICK_S)
    world.write_text("a: 2\n")
    assert _workspace_sha(_at(tmp_path)) != before


def test_a_restored_tree_that_keeps_its_mtime_is_still_a_changed_tree(tmp_path):
    # rsync -t and tar extract both put the old mtime back. ctime is not theirs to set,
    # which is why it is in the fingerprint alongside mtime.
    world = tmp_path / "world.yaml"
    world.write_text("a: 1\n")
    stat = world.stat()
    before = _workspace_sha(_at(tmp_path))
    time.sleep(_PAST_A_TICK_S)
    world.write_text("b: 2\n")
    os.utime(world, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert world.stat().st_mtime_ns == stat.st_mtime_ns
    assert _workspace_sha(_at(tmp_path)) != before


def test_adding_and_removing_a_file_is_a_changed_tree(tmp_path):
    (tmp_path / "world.yaml").write_text("a: 1\n")
    before = _workspace_sha(_at(tmp_path))
    (tmp_path / "extra.yaml").write_text("b: 2\n")
    with_extra = _workspace_sha(_at(tmp_path))
    assert with_extra != before
    (tmp_path / "extra.yaml").unlink()
    assert _workspace_sha(_at(tmp_path)) == before


def test_reading_the_same_untouched_tree_twice_agrees(tmp_path):
    # Otherwise the pool would never hit: a fingerprint that moves on its own is a cold
    # start on every call.
    (tmp_path / "world.yaml").write_text("a: 1\n")
    assert _workspace_sha(_at(tmp_path)) == _workspace_sha(_at(tmp_path))


def test_a_campaign_fingerprints_as_nothing(tmp_path):
    # A campaign stages no workspace and is frozen once it starts, so its id is already an
    # identity. Adding a digest for it would be noise that could differ between two calls
    # for the same frozen campaign.
    spec = _spec()
    assert _workspace_sha(spec) == ""


def test_an_unreadable_workspace_is_not_the_same_as_no_workspace(tmp_path):
    # Returning "" here would let a workspace whose directory has gone missing share a
    # held container with a campaign, which stages an entirely different tree.
    spec = _spec()
    spec.workspace_dir = str(tmp_path / "gone")
    assert _workspace_sha(spec) not in ("",)


# -- and what it does to the identity ----------------------------------------


def test_editing_a_workspace_is_a_different_container(local, monkeypatch):
    # The identity tuple must carry the CONTENTS: a held container's project is staged at
    # creation and exec_in_held cannot restage it, so reusing one for an edited workspace
    # answers from the tree it was started with.
    calls = []

    class _Mgr:
        def run(self, spec, limit_s, *, keep_alive, identity, query=False, fresh=False):
            calls.append(identity)
            return (0, "", "", False)

        def state(self, slot=None):
            return None

    workspace = local.store.registry.create("edited")["workspace_id"]
    project = local.store.registry.project_dir(workspace)
    project.mkdir(parents=True, exist_ok=True)
    world = project / "world.yaml"
    world.write_text("pos: [40, 40]\n")

    monkeypatch.setattr(LocalTransport, "_exec_manager", property(lambda self: _Mgr()))
    monkeypatch.setattr(LocalTransport, "_exec_vast_file",
                        lambda self, request: "x.vast")
    monkeypatch.setattr(
        LocalTransport, "_resolve_exec_image",
        lambda self, vast, container=None, campaign_id="": ImageRef(
            ref="img", identity="img", build_id=""))
    monkeypatch.setattr("robovast.service.container_exec.validate", lambda request: None)
    monkeypatch.setattr("robovast.service.container_exec.stage",
                        lambda *a, **kw: (_spec(), {}, 300, "command"))
    monkeypatch.setattr("robovast.service.container_exec.result_from",
                        lambda out, **kw: out)

    request = ExecRequest(command="true", workspace_id=workspace, query=True)
    local.exec_in_container(request)
    world.write_text("pose: {position: {x: 40, y: 40}}\n")
    local.exec_in_container(request)

    assert calls[0] != calls[1], (
        "an edited workspace reused the container staged from the tree before the edit")


def test_an_untouched_workspace_still_reuses_its_container(local, monkeypatch):
    # The point of the pool: fingerprinting must not make every call a cold start, or a
    # repeat validate would pay a container creation it does not need.
    calls = []

    class _Mgr:
        def run(self, spec, limit_s, *, keep_alive, identity, query=False, fresh=False):
            calls.append(identity)
            return (0, "", "", False)

        def state(self, slot=None):
            return None

    workspace = local.store.registry.create("untouched")["workspace_id"]
    project = local.store.registry.project_dir(workspace)
    project.mkdir(parents=True, exist_ok=True)
    (project / "world.yaml").write_text("pose: {position: {x: 40, y: 40}}\n")

    monkeypatch.setattr(LocalTransport, "_exec_manager", property(lambda self: _Mgr()))
    monkeypatch.setattr(LocalTransport, "_exec_vast_file",
                        lambda self, request: "x.vast")
    monkeypatch.setattr(
        LocalTransport, "_resolve_exec_image",
        lambda self, vast, container=None, campaign_id="": ImageRef(
            ref="img", identity="img", build_id=""))
    monkeypatch.setattr("robovast.service.container_exec.validate", lambda request: None)
    monkeypatch.setattr("robovast.service.container_exec.stage",
                        lambda *a, **kw: (_spec(), {}, 300, "command"))
    monkeypatch.setattr("robovast.service.container_exec.result_from",
                        lambda out, **kw: out)

    request = ExecRequest(command="true", workspace_id=workspace, query=True)
    local.exec_in_container(request)
    local.exec_in_container(request)

    assert calls[0] == calls[1]
