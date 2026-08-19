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
from robovast.service.image_build import BuildSpec
from robovast.service.image_store import (ImageBuildStore, ImageRef, LocalDockerImageStore,
                                          build_identity, local_build_id)

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
