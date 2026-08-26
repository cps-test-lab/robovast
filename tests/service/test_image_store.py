# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The image store: the seam that decides where a lane's built images live.

These are the tests that would have caught the reported bug. Its cause was not a wrong
line -- it was that "where does this image live, and is it there?" had no single owner, so
the cluster lane inherited the local lane's answer (``docker image inspect``, inside a
service pod with no docker) and reported every built image as unbuilt.

So what is asserted here is the *contract*, against every implementation there is: nobody
answers "not there" when it could not ask, and nobody hands a concrete registry ref to a
client.
"""

import subprocess

import pytest

from robovast.common.errors import ImageStoreUnavailable
from robovast.service import image_store
from robovast.service.image_build import BuildSpec
from robovast.service.image_store import (ImageBuildStore, ImageRef, LocalDockerImageStore,
                                          build_identity, local_build_id)

try:
    from robovast.execution.cluster_execution.registry_image_store import RegistryImageStore
except ImportError:                                     # robovast_cluster not installed
    RegistryImageStore = None

SPEC = BuildSpec(tag="sut", base_image="ghcr.io/x/robovast:latest",
                 python_packages=["shapely==2.0.1"])


def _stores(tmp_path):
    """Every :class:`ImageBuildStore` implementation, for the contract tests below.

    The registry store needs no cluster to answer ``ref_for``: it is given a config and a
    client, both stubbed. A new lane belongs in this list -- that is the point of it.
    """
    stores = [LocalDockerImageStore(tmp_path / "builds")]
    try:
        from robovast.execution.cluster_execution.registry_image_store import \
            RegistryImageStore
    except ImportError:                                     # robovast_cluster not installed
        return stores

    class _Registry:
        registry_prefix = "registry.local:5000/robovast"
        base_experiment_image = "registry.local:5000/robovast/base:1"
        # Named explicitly, which the store honours without any API lookup -- so this
        # stub needs no Kubernetes at all to answer a resolution question.
        push_secret_name = "robovast-registry"
        ca_configmap_name = "robovast-registry-ca"
        insecure = True
        pull_secret_name = "robovast-registry"

        def enabled(self):
            return True

        def why_disabled(self):
            return ""

    class _Cfg:
        def get_registry_config(self):
            return _Registry()

    class _NoObjects:
        """A cluster where the optional Secret/ConfigMap are simply absent.

        Not a mock of success: an absent credential is the case a deployment with a public
        registry is genuinely in, and it must not stop the store answering.
        """

        def _absent(self, *_a, **_kw):
            from kubernetes.client.exceptions import ApiException
            raise ApiException(status=404, reason="Not Found")

        read_namespaced_secret = _absent
        read_namespaced_config_map = _absent

    stores.append(RegistryImageStore("default", _Cfg, _NoObjects))
    return stores


# ---------------------------------------------------------------------------
# The contract every store owes its callers
# ---------------------------------------------------------------------------

def test_a_store_that_forgets_a_method_cannot_be_built():
    """An ABC and not a Protocol, deliberately: a structural check would let an incomplete
    lane fail at whichever call site reached it first, which is how this bug behaved."""
    class Forgetful(ImageBuildStore):
        def ref_for(self, spec, project_dir):
            return ImageRef(ref="x", identity="build:x@h", build_id="b")

    with pytest.raises(TypeError, match="present"):
# the TypeError this asserts is the point
        # pylint: disable-next=abstract-class-instantiated
        Forgetful()


def test_every_store_names_an_image_without_naming_a_registry(tmp_path):
    """``identity`` crosses the API boundary; ``ref`` must not. The registry store's ref is
    registry-qualified by construction, so this is the assertion that keeps it inside."""
    for store in _stores(tmp_path):
        found = store.ref_for(SPEC, tmp_path)
        assert found.identity.startswith("build:sut@")
        assert "registry.local" not in found.identity
        assert "/" not in found.identity.split("@")[0]
        assert found.image_hash and found.image_hash in found.identity
        assert found.build_id


def test_every_store_hashes_its_own_base(tmp_path):
    """The base image is part of the cache key, and each lane may supply a different one --
    the cluster folds its registry's base in. That is why one store must own the derivation
    rather than each caller repeating it: two derivations of "the same" hash disagreed, and a
    built image came back unbuilt."""
    spec_without_base = BuildSpec(tag="sut", python_packages=["shapely==2.0.1"])
    for store in _stores(tmp_path):
        assert (store.ref_for(SPEC, tmp_path).image_hash
                != store.ref_for(spec_without_base, tmp_path).image_hash)


# ---------------------------------------------------------------------------
# "I could not ask" is never reported as "it is not there"
# ---------------------------------------------------------------------------

def test_the_local_store_refuses_rather_than_blaming_the_image(tmp_path, monkeypatch):
    """The exact misdiagnosis behind the report: no docker CLI here, reported as an unbuilt
    image. It cost an investigation, because the answer named the wrong layer entirely."""
    store = LocalDockerImageStore(tmp_path / "builds")
    ref = store.ref_for(SPEC, tmp_path)

    def _no_docker(*a, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", _no_docker)
    with pytest.raises(ImageStoreUnavailable) as excinfo:
        store.present(ref)
    assert "docker CLI is not usable here" in str(excinfo.value)
    assert "not something a rebuild fixes" in str(excinfo.value)


def test_the_local_store_reports_a_genuinely_absent_image_as_absent(tmp_path, monkeypatch):
    store = LocalDockerImageStore(tmp_path / "builds")
    ref = store.ref_for(SPEC, tmp_path)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 1, b"", b""))
    assert store.present(ref) is False
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 0, b"[]", b""))
    assert store.present(ref) is True


def test_a_registry_that_did_not_answer_is_not_an_unbuilt_image(tmp_path, monkeypatch):
    """``manifest_exists`` deliberately fails closed for the *build* path. A caller asking
    whether it can RUN the image needs the opposite, so the store reads the tri-state."""
    ris = pytest.importorskip(
        "robovast.execution.cluster_execution.registry_image_store")
    store = [s for s in _stores(tmp_path)
             if isinstance(s, ris.RegistryImageStore)][0]
    ref = store.ref_for(SPEC, tmp_path)

    monkeypatch.setattr(ris, "manifest_state", lambda *a, **kw: "unknown")
    with pytest.raises(ImageStoreUnavailable, match="did not answer"):
        store.present(ref)
    monkeypatch.setattr(ris, "manifest_state", lambda *a, **kw: "absent")
    assert store.present(ref) is False
    monkeypatch.setattr(ris, "manifest_state", lambda *a, **kw: "present")
    assert store.present(ref) is True


def test_the_local_build_id_is_the_one_start_records(tmp_path):
    """A refusal derives the build id from the spec; ``start`` records the build under it.
    If those two ever differ, the id in "wait for this build" names nothing."""
    store = LocalDockerImageStore(tmp_path / "builds")
    found = store.ref_for(SPEC, tmp_path)
    assert found.build_id == local_build_id("sut", found.image_hash)
    assert found.identity == build_identity("sut", found.image_hash)


def test_every_store_folds_the_vcs_resolution_into_its_hash(tmp_path, monkeypatch):
    """A moving ref must change the key on EVERY lane, not just the one that remembered.

    The cluster lane hashed without the resolution, so a spec naming a branch was
    cache-stable there: the first build's commit was served for ever, and because the same
    omission left the Dockerfile unpinned, the image installed whatever the branch pointed at
    when the Job ran -- with nothing recording which commit that was. The local lane did it
    correctly, which is precisely why nothing failed and nobody noticed.
    """
    from robovast.service import image_build

    spec = BuildSpec(tag="sut", base_image="ghcr.io/x/robovast:latest",
                     python_packages=["pkg @ git+https://host/repo@main"])

    def _hash_when_the_branch_points_at(store, sha):
        monkeypatch.setattr(image_build, "_ls_remote", lambda *_a, **_kw: sha)
        return store.ref_for(spec, tmp_path).image_hash

    for store in _stores(tmp_path):
        before = _hash_when_the_branch_points_at(store, "a" * 40)
        after = _hash_when_the_branch_points_at(store, "b" * 40)
        assert before != after, (
            f"{type(store).__name__}.ref_for ignores the resolution: a moved branch is a "
            f"cache hit, so the first build is served for ever")


def test_every_store_folds_the_base_it_would_build_on_into_its_hash(tmp_path, monkeypatch):
    """A republished base must change the key on EVERY lane, not just the one that remembered.

    Same shape as the vcs omission above and the same lane: the cluster store hashed the base
    *ref*, so republishing what a floating tag points at was invisible to it -- a new simulator
    image, or a refreshed apt snapshot, and every derived experiment image stayed a cache hit,
    built on the base of whichever day it was first built. On the lane campaigns actually run on,
    that is the silent substitution :func:`build_hash` exists to prevent, and it looks exactly
    like a run that did use the new base.
    """
    ris = pytest.importorskip(
        "robovast.execution.cluster_execution.registry_image_store")

    def _hash_when_the_base_is(store, identity):
        if isinstance(store, ris.RegistryImageStore):
            monkeypatch.setattr(type(store), "published_digest",
                                lambda _self, _ref: identity)
        else:
            monkeypatch.setattr(image_store, "local_image_id", lambda _ref: identity)
        return store.ref_for(SPEC, tmp_path).image_hash

    for store in _stores(tmp_path):
        before = _hash_when_the_base_is(store, "sha256:" + "a" * 64)
        after = _hash_when_the_base_is(store, "sha256:" + "b" * 64)
        assert before != after, (
            f"{type(store).__name__}.ref_for hashes the base REF: a republished base is a cache "
            f"hit, so the image keeps the base it was first built on"
        )


def test_a_base_the_registry_cannot_resolve_still_answers(tmp_path, monkeypatch):
    """An unreachable registry must not become a forced rebuild on every call: the ref is what
    the key held before the digest was folded in, so falling back to it keeps that behaviour."""
    ris = pytest.importorskip(
        "robovast.execution.cluster_execution.registry_image_store")
    store = [s for s in _stores(tmp_path) if isinstance(s, ris.RegistryImageStore)][0]
    monkeypatch.setattr(type(store), "published_digest", lambda _self, _ref: "")
    first = store.ref_for(SPEC, tmp_path).image_hash
    assert first and store.ref_for(SPEC, tmp_path).image_hash == first


def test_the_resolution_is_shared_rather_than_reimplemented_per_lane():
    """Concrete on the ABC, because "which commit does this ref name?" is not a property of
    where images are stored. While it lived on one store, the other simply did without it."""
    for store_cls in (LocalDockerImageStore, *([RegistryImageStore] if RegistryImageStore else [])):
        assert store_cls.resolve_vcs is ImageBuildStore.resolve_vcs
