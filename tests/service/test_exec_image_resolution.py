# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which image an ``exec_in_container`` runs, and what it says when there is none.

The reported bug lived here. A deployment on the cluster lane answered
``build_experiment_image`` with ``cached: true`` and then refused the very next
``exec_in_container`` with "the image for container 'sut' is not built", for three different
call shapes -- because ``_exec_image`` was implemented once, for the local docker daemon, and
inherited unchanged by a lane whose images live in a registry and whose service pod has no
docker at all. Two separate faults, one per config source:

* a **workspace** source asked the wrong store, and got "absent" from a probe that had in
  fact failed to run;
* a **campaign** source re-derived a content hash from the campaign's frozen ``_config/``,
  which holds the ``.vast`` and the run files but not the build inputs -- so every source dir
  and workspace wheel hashed as a bare requirement and the hash could not match the build's.

So these tests are about the two questions the old code conflated: *which store answers*, and
*what the caller is told when the answer is no*.
"""

import types

import pytest

from robovast.common.errors import ImageNotBuilt, ImageStoreUnavailable
from robovast.service.client import LocalTransport
from robovast.service.image_store import ImageRef
from robovast.service.interface import ImageBuildStatus
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

BUILT = ImageRef(ref="registry.local:5000/robovast/sut:abc123",
                 identity="build:sut@abc123", build_id="imgbuild-sut-abc123",
                 image_hash="abc123")


def _vast(tmp_path, packages=("shapely>=2.0",)):
    """A project whose ``sut`` container adds packages, so it has an image to build."""
    import json
    vast = tmp_path / "p.vast"
    vast.write_text(json.dumps({
        "version": 2,
        "execution": {"runs": 1, "containers": {
            "scenario": {"image": "base:1"},
            "sut": {"image": "base:1", "python_packages": list(packages)}}}}))
    return str(vast)


def _transport(tmp_path, store):
    t = LocalTransport(store=WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path))))
    t._image_store = store
    return t


class _Store:
    """A store that resolves, and answers ``present`` however the test says."""

    def __init__(self, present=True):
        self._present = present

    def ref_for(self, spec, project_dir):
        return BUILT

    def present(self, ref):
        if isinstance(self._present, Exception):
            raise self._present
        return self._present


# ---------------------------------------------------------------------------
# which store answers
# ---------------------------------------------------------------------------

def test_the_lanes_own_store_resolves_the_image(tmp_path):
    """The fix for the report: resolution goes through whichever store the lane installed,
    so a cluster deployment is answered by its registry and not by a docker daemon it has
    never had."""
    t = _transport(tmp_path, _Store(present=True))
    assert t._exec_image(_vast(tmp_path), "sut") == BUILT.ref


def test_a_client_is_told_the_identity_and_never_the_concrete_ref(tmp_path):
    """``resolve_image`` crosses the API boundary — it keys the per-image catalog cache and
    is reported to the caller — and on this lane the concrete ref names a registry."""
    t = _transport(tmp_path, _Store(present=True))
    vast = _vast(tmp_path)
    # Which .vast a request names is resolved before this point and is not what is under
    # test; a workspace lookup here would only be asserting workspace plumbing.
    t._exec_vast_file = lambda request: vast
    request = types.SimpleNamespace(workspace_id="ws1", config_path=vast,
                                    campaign_id="", container="sut")
    resolved = t.resolve_image(request).image
    assert resolved == "build:sut@abc123"
    assert "registry.local" not in resolved


def test_a_container_that_builds_nothing_needs_no_store(tmp_path):
    """Most containers run their image as declared. That path never had the bug and must not
    acquire a dependency on the store."""
    t = _transport(tmp_path, _Store(present=Exception("the store must not be asked")))
    assert t._exec_image(_vast(tmp_path), "scenario") == "base:1"


def test_a_store_that_could_not_answer_is_not_reported_as_an_unbuilt_image(tmp_path):
    """The misdiagnosis itself. ``ImageStoreUnavailable`` must reach the caller as what it
    is — an infrastructure problem — not be recycled into "call build_experiment_image",
    which is the advice that sent the reporter round the loop three times."""
    t = _transport(tmp_path, _Store(present=ImageStoreUnavailable("no docker here")))
    with pytest.raises(ImageStoreUnavailable, match="no docker here"):
        t._exec_image(_vast(tmp_path), "sut")


# ---------------------------------------------------------------------------
# what the caller is told when the answer is no
# ---------------------------------------------------------------------------

def _refusal(tmp_path, status):
    t = _transport(tmp_path, _Store(present=False))
    t.get_image_build_status = lambda build_id: (
        status if status is not None else _raise(KeyError(build_id)))
    with pytest.raises(ImageNotBuilt) as excinfo:
        t._exec_image(_vast(tmp_path), "sut")
    return excinfo.value


def _raise(exc):
    raise exc


def test_nothing_started_says_build_it(tmp_path):
    err = _refusal(tmp_path, None)
    assert "no build is running" in str(err)
    assert err.next_step == "build_experiment_image(container='sut')"


def test_a_build_in_flight_says_wait_and_names_it(tmp_path):
    """The state an agent lands in when it execs straight after a build — and the one the
    old single message could not distinguish from "you forgot to build"."""
    err = _refusal(tmp_path, ImageBuildStatus(
        build_id=BUILT.build_id, tag="sut", phase="building",
        started_at="2026-08-18T10:00:00Z"))
    assert "still building" in str(err)
    assert "vast image wait imgbuild-sut-abc123" in err.next_step


def test_a_failed_build_sends_the_caller_to_the_diagnosis(tmp_path):
    err = _refusal(tmp_path, ImageBuildStatus(
        build_id=BUILT.build_id, tag="sut", phase="failed", done=True))
    assert "failed to build" in str(err)
    assert "get_image_build_log" in err.next_step


def test_a_build_state_that_cannot_be_read_still_refuses(tmp_path):
    """On the cluster the state lookup touches the API server. A failure there is not an
    answer about the image, and must not replace the refusal with something the caller
    cannot act on — the decoration degrades, the refusal stands."""
    t = _transport(tmp_path, _Store(present=False))

    def _boom(build_id):
        raise RuntimeError("api server unreachable")

    t.get_image_build_status = _boom
    with pytest.raises(ImageNotBuilt) as excinfo:
        t._exec_image(_vast(tmp_path), "sut")
    assert excinfo.value.next_step == "build_experiment_image(container='sut')"


# ---------------------------------------------------------------------------
# a campaign exec runs what the campaign ran
# ---------------------------------------------------------------------------

def test_a_campaign_source_uses_what_the_campaign_recorded(tmp_path, monkeypatch):
    """The second bug. A campaign's ``_config/`` carries no build inputs, so re-deriving the
    hash there could never match the build — and the recorded image is the better answer
    anyway: it is the bytes that actually ran, so the diagnostic is about that campaign
    rather than about what the workspace would build today."""
    t = _transport(tmp_path, _Store(present=Exception("the store must not be asked")))
    monkeypatch.setattr("robovast.common.campaign_data.campaign_role_image",
                        lambda campaign_dir, role, resolve_digest=None: "sha256:deadbeef")
    found = t._resolve_exec_image(_vast(tmp_path), "sut", campaign_id="campaign-1")
    assert found.ref == "sha256:deadbeef"
    # A digest names bytes and no registry we chose, so it is its own identity.
    assert found.identity == "sha256:deadbeef"


def test_the_campaign_dir_is_a_seam_and_not_the_whole_campaign(tmp_path, monkeypatch):
    """``_data_dir`` refuses on the cluster lane on purpose — while it silently answered "the
    whole campaign", every inherited caller became a whole-campaign download. So this path
    names its own objects, and the cluster override materialises exactly those.

    Asserted by inspection because constructing the real thing needs a cluster; what has to
    exist is the override at all, since inheriting the local answer would read a directory
    the cluster does not have — the same shape of bug as the one being fixed here.
    """
    import inspect

    from robovast.execution.cluster_execution.cluster_service import ClusterService
    source = inspect.getsource(ClusterService._role_image_source_dir)
    assert "_retrigger_source_dir" in source, "it must materialise before reading"
    assert "_data_dir" not in source


def test_the_role_image_lookup_reads_the_seams_directory(tmp_path, monkeypatch):
    """And the caller must actually use it, or the override above is decoration."""
    t = _transport(tmp_path, _Store(present=True))
    seen = {}
    t._role_image_source_dir = lambda cid: seen.setdefault("dir", f"/materialised/{cid}")
    monkeypatch.setattr(
        "robovast.common.campaign_data.campaign_role_image",
        lambda campaign_dir, role, resolve_digest=None: (
            seen.setdefault("read", str(campaign_dir)) and "sha256:aa") or "sha256:aa")
    t._resolve_exec_image(_vast(tmp_path), "sut", campaign_id="campaign-1")
    assert seen["read"] == "/materialised/campaign-1"
