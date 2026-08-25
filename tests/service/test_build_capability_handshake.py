# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A service says in the handshake whether it can build an experiment image.

A campaign whose container adds packages needs somewhere to push the derived image. On the
cluster lane that is the service's own in-pod registry, reached over its Ingress -- so a
service that is unpublished, or whose registry prefix a ``setup`` re-run dropped, cannot
build at all. Nothing said so until ``start_campaign``, after a project push, a workspace
create and a launch.

Two properties carry it, and both are easy to break in a way that makes things worse than
saying nothing:

* **``None`` means "no verdict".** A service older than the field leaves it absent. Reading
  that as ``False`` would tell every healthy pre-field deployment to go and fix a registry.
* **It is capability, not liveness.** The local lane answers ``True`` without touching
  Docker. Probing would put a 15 s subprocess timeout in the one call whose job is to answer
  instantly, and a dead daemon is a different question with three other answers.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

import types
from unittest.mock import MagicMock, patch

import pytest

from robovast.execution.cluster_config.base_config import RegistryConfig
from robovast.service.interface import VersionInfo


def test_a_service_that_did_not_say_leaves_both_absent():
    """The compatibility default. Round-trips a payload with neither field."""
    v = VersionInfo.model_validate({"robovast_version": "2.0.0"})

    assert v.can_build_images is None
    assert v.build_unavailable is None


def test_extra_keys_from_a_newer_service_do_not_break_an_older_client():
    """The other direction: pydantic ignores what it does not know."""
    v = VersionInfo.model_validate({"robovast_version": "2.0.0",
                                    "some_field_from_the_future": 7})
    assert v.robovast_version == "2.0.0"


# -- the local lane ----------------------------------------------------------


@pytest.fixture
def local():
    """A LocalTransport with only what `version()` reads."""
    from robovast.service.client import LocalTransport

    lt = LocalTransport.__new__(LocalTransport)
    # `store` is a plain instance attribute; `_campaigns_root` is a method.
    lt.store = types.SimpleNamespace(registry=types.SimpleNamespace(root="/tmp/w"))
    with patch.object(LocalTransport, "_campaigns_root", return_value="/tmp/c"):
        yield lt


def test_the_local_lane_can_always_build(local):
    """It builds with `docker buildx --load` into the local daemon: no registry, no
    Ingress, nothing an operator can misconfigure."""
    v = local.version()

    assert v.can_build_images is True
    assert v.build_unavailable is None


def test_it_does_not_probe_docker(local, monkeypatch):
    """The regression that matters. `check_docker_access` shells out with a 15 s timeout,
    and this is the call that must answer instantly."""
    import subprocess

    def _boom(*_a, **_k):
        raise AssertionError("version() spawned a subprocess to answer a capability question")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    assert local.version().can_build_images is True


# -- the cluster lane --------------------------------------------------------


def _cluster_version(registry: RegistryConfig) -> VersionInfo:
    """`ClusterService.version()` with only the registry config supplied."""
    from robovast.execution.cluster_execution.cluster_service import ClusterService

    cs = ClusterService.__new__(ClusterService)
    with patch.object(ClusterService, "version", ClusterService.version), \
            patch("robovast.service.local_transport.LocalTransport.version",
                  return_value=VersionInfo(robovast_version="2.0.0")), \
            patch.object(ClusterService, "_api_server_url", return_value=None), \
            patch.object(ClusterService, "_cluster_config",
                         return_value=types.SimpleNamespace(
                             get_registry_config=lambda: registry)):
        cs.kube_context = None
        cs._kube_context_source = "active kubeconfig context"  # noqa: SLF001
        cs.namespace = "default"
        return cs.version()


def test_a_cluster_with_a_registry_can_build():
    v = _cluster_version(RegistryConfig(registry_prefix="robovast.example.org"))

    assert v.can_build_images is True
    assert v.build_unavailable is None, "no reason to give when it works"


def test_a_cluster_without_one_says_why_and_how_to_fix_it():
    v = _cluster_version(RegistryConfig(registry_prefix=""))

    assert v.can_build_images is False
    assert v.build_unavailable
    # Both remedies, because the in-pod service cannot tell the two states apart.
    # pylint: disable-next=unsupported-membership-test  -- build_unavailable is asserted truthy three lines up
    assert "vast exec cluster upgrade" in v.build_unavailable
    # pylint: disable-next=unsupported-membership-test  -- as above
    assert "vast exec cluster setup" in v.build_unavailable


def test_the_reason_carries_no_registry_detail():
    """Registry endpoints and credentials do not cross this interface, and this field
    does. A prefix pasted into the message would be the easiest way to break that."""
    v = _cluster_version(RegistryConfig(registry_prefix="",
                                        push_secret_name="robovast-registry-push"))

    assert "robovast-registry-push" not in (v.build_unavailable or "")


def test_an_unreadable_config_is_no_verdict_rather_than_a_false_one():
    """"I could not tell" is not "you cannot build". Reporting False on a config-loading
    problem would send an operator to fix a registry that is fine."""
    from robovast.execution.cluster_execution.cluster_service import ClusterService

    cs = ClusterService.__new__(ClusterService)
    with patch("robovast.service.local_transport.LocalTransport.version",
               return_value=VersionInfo(robovast_version="2.0.0")), \
            patch.object(ClusterService, "_api_server_url", return_value=None), \
            patch.object(ClusterService, "_cluster_config",
                         side_effect=RuntimeError("no config")):
        cs.kube_context = None
        cs._kube_context_source = "active kubeconfig context"  # noqa: SLF001
        cs.namespace = "default"
        v = cs.version()

    assert v.can_build_images is None
    assert v.build_unavailable is None


# -- the MCP relay -----------------------------------------------------------


def _service_info(version: VersionInfo) -> dict:
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins.reference import get_service_info

    client = MagicMock()
    client.version.return_value = version
    with patch.object(service_access, "service_client", return_value=client):
        return get_service_info()


def test_the_relay_omits_a_verdict_that_was_not_given():
    """Matching the omit-null rule the roots already follow: a null field reads as
    "unknown" where the truthful statement is "this service did not say"."""
    info = _service_info(VersionInfo(robovast_version="2.0.0"))

    assert "can_build_images" not in info
    assert "build_unavailable" not in info


def test_the_relay_passes_a_true_verdict_without_a_reason():
    info = _service_info(VersionInfo(robovast_version="2.0.0", can_build_images=True))

    assert info["can_build_images"] is True
    assert "build_unavailable" not in info, "there is no reason when it works"


def test_the_relay_passes_a_false_verdict_with_its_reason():
    info = _service_info(VersionInfo(robovast_version="2.0.0", can_build_images=False,
                                     build_unavailable="nowhere to push it. …"))

    assert info["can_build_images"] is False
    assert "nowhere to push it" in info["build_unavailable"]
