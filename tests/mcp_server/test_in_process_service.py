# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A mounted MCP calls the service it lives in, not itself over HTTP.

``vast serve`` mounts the MCP app on its own port, so the tools and the implementation
are in one process. They still went out over loopback and back in — a wasted round trip
per tool call, and once a token was required, a process authenticating to itself that
worked only because it happened to hold its own secret.
"""

from types import SimpleNamespace

import pytest

from robovast.mcp_server import service_access


@pytest.fixture(autouse=True)
def _unmount():
    """Leave the module global as it was found.

    ``use_in_process_service`` is process-wide by design (set once at app construction),
    so a test that mounts one and does not undo it decides what every later test in the
    session sees -- including tests in other files, which then pass or fail on ordering.
    """
    before = service_access._IN_PROCESS  # noqa: SLF001
    yield
    service_access.use_in_process_service(before)


def test_without_mounting_the_client_is_resolved_over_http(monkeypatch):
    monkeypatch.setattr("robovast.client.service_target.detected_service_url",
                        lambda: "https://robovast.example.org")
    client = service_access.service_client()
    assert getattr(client, "base_url", None) == "https://robovast.example.org"


def test_no_service_and_no_mount_is_none(monkeypatch):
    monkeypatch.setattr("robovast.client.service_target.detected_service_url",
                        lambda: "")
    assert service_access.service_client() is None


def test_a_mounted_mcp_gets_the_implementation_itself(monkeypatch):
    sentinel = object()
    service_access.use_in_process_service(sentinel)

    def _must_not_be_called():
        raise AssertionError("a mounted MCP must not resolve a URL to reach itself")

    monkeypatch.setattr("robovast.client.service_target.detected_service_url",
                        _must_not_be_called)
    assert service_access.service_client() is sentinel


def test_building_the_app_with_mcp_binds_the_implementation():
    from robovast.service.app import build_app
    from robovast.service.client import LocalTransport

    impl = LocalTransport()
    build_app(impl, mount_mcp=True, auth_token="tok")
    assert service_access.service_client() is impl


# -- web_url: which origin a link is built from ------------------------------


def _info(**kw):
    from robovast.service.interface import VersionInfo
    return VersionInfo(robovast_version="test", **kw)


def test_a_dialled_base_url_wins_over_the_declaration():
    """What demonstrably reached the service beats what the service believes.

    A ``vast serve`` behind a tunnel or a port-forward declares the address it bound --
    right for itself, useless to this caller -- while ``base_url`` is the address that just
    worked. Preferring the declaration would break the case that works today.
    """
    client = SimpleNamespace(
        base_url="https://tunnel.example.org",
        version=lambda: _info(web_base="http://127.0.0.1:8800"))
    assert service_access.web_url(client, "/campaigns/c/archive") == \
        "https://tunnel.example.org/campaigns/c/archive"


def test_a_mounted_impl_uses_the_declared_origin():
    impl = SimpleNamespace(version=lambda: _info(web_base="https://robovast.example.org"))
    service_access.use_in_process_service(impl)
    assert service_access.web_url(impl, "/campaigns/c/archive") == \
        "https://robovast.example.org/campaigns/c/archive"


def test_no_transport_and_no_declaration_gives_no_url():
    """An unpublished deployment has no origin, and says so by omission.

    The caller's ``path``/address is still usable; a link built from nothing would not be.
    """
    impl = SimpleNamespace(version=lambda: _info())
    service_access.use_in_process_service(impl)
    assert service_access.web_url(impl, "/campaigns/c/archive") == ""


def test_a_failed_version_lookup_costs_the_link_not_the_answer():
    """A link is an extra route to a payload; losing it must not lose the reply."""
    def _raise():
        raise RuntimeError("service unreachable")

    impl = SimpleNamespace(version=_raise)
    service_access.use_in_process_service(impl)
    assert service_access.web_url(impl, "/campaigns/c/archive") == ""


def test_a_service_too_old_to_declare_one_is_not_an_error():
    """``web_base`` absent from the response model reads as "none", like every other
    conditional field in ``VersionInfo``."""
    impl = SimpleNamespace(version=lambda: SimpleNamespace(robovast_version="old"))
    service_access.use_in_process_service(impl)
    assert service_access.web_url(impl, "/campaigns/c/archive") == ""
