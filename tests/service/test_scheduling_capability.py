# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A service says in the handshake whether it queues campaigns (``can_schedule``).

The web UI offers priority and pause only where this is true, so it has to be the same flag
the service refuses on -- a second answer to "does this service have a queue" would sooner or
later offer an entry the service then refuses, or hide one it would accept.
"""

import types

import pytest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from robovast.service.interface import VersionInfo


def _null_service():
    from tests.service.null_service import NullService

    service = object.__new__(NullService)
    service.store = types.SimpleNamespace(registry=types.SimpleNamespace(root="/tmp/w"))
    return service


def test_a_service_that_did_not_say_has_no_verdict():
    assert VersionInfo.model_validate({"robovast_version": "2.0.0"}).can_schedule is None


def test_a_service_without_a_queue_says_it_has_none():
    from tests.service.null_service import NullService

    with patch.object(NullService, "_campaigns_root", return_value="/tmp/c"):
        assert _null_service().version().can_schedule is False


def test_the_answer_is_the_predicate_the_service_refuses_on():
    """One source of truth: flip the implementation's own answer and both follow it.

    The handshake and the admission read the same ``_queues_campaigns``, so a client is
    never offered an entry the service would refuse, nor denied one it would accept.
    """
    from robovast.service.interface import CreateCampaignRequest
    from tests.service.null_service import NullService

    with patch.object(NullService, "_campaigns_root", return_value="/tmp/c"), \
            patch.object(NullService, "_queues_campaigns", return_value=True):
        service = _null_service()
        assert service.version().can_schedule is True
        # and the refusal is gone with it, rather than left saying the opposite
        service._admit_scheduling(CreateCampaignRequest(workspace_id="ws-x", priority=3))


def test_a_service_without_a_queue_says_so_and_refuses_in_the_same_breath():
    from robovast.service.interface import CreateCampaignRequest, UnsupportedOperation
    from tests.service.null_service import NullService

    with patch.object(NullService, "_campaigns_root", return_value="/tmp/c"):
        service = _null_service()
        assert service.version().can_schedule is False
        with pytest.raises(UnsupportedOperation):
            service._admit_scheduling(CreateCampaignRequest(workspace_id="ws-x", priority=3))


def test_the_cluster_service_says_it_has_one():
    from robovast.execution.cluster_execution.cluster_service import ClusterService
    from robovast.execution.cluster_config.base_config import RegistryConfig

    cs = ClusterService.__new__(ClusterService)
    cs.store = types.SimpleNamespace(registry=types.SimpleNamespace(root="/tmp/w"))
    cs.kube_context = None
    cs._kube_context_source = "active kubeconfig context"  # noqa: SLF001
    cs.namespace = "default"
    with patch.object(ClusterService, "_campaigns_root", return_value="/tmp/c"), \
            patch.object(ClusterService, "_api_server_url", return_value=None), \
            patch.object(ClusterService, "_declared_web_base", return_value=""), \
            patch.object(ClusterService, "_cluster_config",
                         return_value=types.SimpleNamespace(
                             get_registry_config=lambda: RegistryConfig(registry_prefix=""))):
        assert cs.version().can_schedule is True


def _service_info(version: VersionInfo) -> dict:
    from robovast.mcp_server import service_access
    from robovast.mcp_server.plugins.reference import get_service_info

    client = MagicMock()
    client.version.return_value = version
    with patch.object(service_access, "service_client", return_value=client):
        return get_service_info()


def test_the_mcp_relay_passes_a_verdict_and_omits_a_missing_one():
    assert _service_info(VersionInfo(robovast_version="2", can_schedule=True))["can_schedule"] is True
    assert _service_info(VersionInfo(robovast_version="2", can_schedule=False))["can_schedule"] is False
    assert "can_schedule" not in _service_info(VersionInfo(robovast_version="2"))


def _service_info_cli(version: VersionInfo, monkeypatch) -> str:
    import contextlib

    from robovast.client import service_cli

    class _Client:
        def version(self):
            return version

    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(service_cli, "service_client", _client)
    result = CliRunner().invoke(service_cli.service, ["info"])
    assert result.exit_code == 0, result.output
    return result.output


def test_service_info_prints_the_queue_only_when_the_service_said(monkeypatch):
    assert "priority and pause" in _service_info_cli(
        VersionInfo(robovast_version="2", can_schedule=True), monkeypatch)
    assert "one campaign at a time" in _service_info_cli(
        VersionInfo(robovast_version="2", can_schedule=False), monkeypatch)
    assert "queue" not in _service_info_cli(VersionInfo(robovast_version="2"), monkeypatch)
