# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The origin a caller can fetch from is the service's fact, and it declares it.

A transport's ``base_url`` is where *it* dials. That is the same string as "where a caller
can fetch" only for an HTTP client, and only by accident -- the MCP mounted inside the
service has no transport at all, so reading one there hands back an ``AttributeError``
instead. The service reports the origin itself, from the one input that knows it per deployment:
the Ingress it was published on, or the address it bound.
"""

from robovast.service.local_transport import LocalTransport


def _impl(tmp_path):
    return LocalTransport(workspace_dir=str(tmp_path))


def test_a_service_nobody_told_declares_nothing(tmp_path, monkeypatch):
    """Absent, not a guess. A wildcard-bound service genuinely does not know which of its
    addresses a caller used, and an unpublished one has no external address at all."""
    monkeypatch.delenv("ROBOVAST_PUBLIC_URL", raising=False)
    assert _impl(tmp_path).version().web_base == ""


def test_the_deploy_time_origin_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOVAST_PUBLIC_URL", "https://robovast.example.org")
    assert _impl(tmp_path).version().web_base == "https://robovast.example.org"


def test_whitespace_is_not_an_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOVAST_PUBLIC_URL", "  ")
    assert _impl(tmp_path).version().web_base == ""


def test_an_empty_env_value_is_not_an_origin(tmp_path, monkeypatch):
    """``setup`` emits the var *empty* rather than omitting it, so that dropping an Ingress
    resets the pod instead of leaving a stale origin behind in a merge patch. Empty has to
    read as "none declared" for that to work."""
    monkeypatch.setenv("ROBOVAST_PUBLIC_URL", "")
    assert _impl(tmp_path).version().web_base == ""


def test_serve_publishes_the_origin_the_same_way_a_deployment_does(tmp_path, monkeypatch):
    """One input, whoever fills it in.

    ``serve`` knows the bound address and nothing else does, but it does not write onto the
    implementation to say so -- it takes a ``RobovastInterface``, and the origin is not part
    of that contract. It publishes it where a deployed service already receives it, so there
    is one input to read and no precedence to get wrong.
    """
    from robovast.service.app import PUBLIC_URL_ENV, bound_origin
    monkeypatch.delenv(PUBLIC_URL_ENV, raising=False)
    monkeypatch.setenv(PUBLIC_URL_ENV, bound_origin("127.0.0.1", 8800))  # what serve does
    assert _impl(tmp_path).version().web_base == "http://127.0.0.1:8800"


def test_a_baked_origin_is_not_overwritten_by_the_bound_one(tmp_path, monkeypatch):
    """In a pod both exist and only one is reachable: it binds 0.0.0.0 and is reached
    through its Ingress. ``setdefault`` is what makes the baked value win."""
    import os

    from robovast.service.app import PUBLIC_URL_ENV, bound_origin
    monkeypatch.setenv(PUBLIC_URL_ENV, "https://robovast.example.org")
    os.environ.setdefault(PUBLIC_URL_ENV, bound_origin("0.0.0.0", 8800))  # noqa: S104
    assert _impl(tmp_path).version().web_base == "https://robovast.example.org"


def test_the_cluster_lane_needs_no_override_of_its_own():
    """Resolved once in the base class, on purpose.

    The cluster lane runs both in-pod (env) and off-cluster through a port-forward (bound
    address). A lane-local assignment would have had to remember not to blank the second
    case, which is exactly the bug that shape invites -- so it inherits instead.
    """
    from robovast.execution.cluster_execution.cluster_service import ClusterService
    assert ClusterService._declared_web_base is LocalTransport._declared_web_base


def test_a_named_bind_is_an_origin_and_a_wildcard_is_not():
    """``serve`` is the only thing that knows the bound address -- and the only thing that
    knows whether it names one address or all of them.

    The wildcard case is not a missing feature: it is the pod, where the environment
    carries the origin and a loopback guess here would overwrite it with a dead one.
    """
    from robovast.service.app import bound_origin
    assert bound_origin("127.0.0.1", 8800) == "http://127.0.0.1:8800"
    assert bound_origin("192.168.1.10", 8801) == "http://192.168.1.10:8801"
    assert bound_origin("0.0.0.0", 8800) == ""  # noqa: S104
    assert bound_origin("::", 8800) == ""
