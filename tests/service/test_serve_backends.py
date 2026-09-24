# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast serve`` resolves its implementation instead of importing one.

Listing what is installed imports none of it, and ``ServiceBase`` loads no driver, so a core
without the cluster code reports what is missing rather than failing on an import.
"""

import subprocess
import sys
import textwrap

import pytest

from robovast.service import serve_backends


def _imports_after(statement: str) -> set:
    """Modules present in a *fresh* interpreter after running ``statement``."""
    script = textwrap.dedent(f"""
        import json, sys
        {statement}
        print(json.dumps(sorted(sys.modules)))
    """)
    out = subprocess.run([sys.executable, "-c", script],
                         capture_output=True, text=True, check=True)
    import json
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_the_cluster_implementation_is_registered():
    assert "cluster" in serve_backends.available()


def test_the_installed_implementation_is_the_one_served():
    name, backend = serve_backends.resolve()
    assert name == "cluster"
    assert isinstance(backend, serve_backends.ServeBackend)
    assert backend.storage


def test_none_installed_names_the_distribution_that_ships_one(monkeypatch):
    """A core alone serves nothing, and says which package to install."""
    monkeypatch.setattr(serve_backends, "available", lambda: {})
    with pytest.raises(ValueError, match="robovast-cluster"):
        serve_backends.resolve()


def test_several_installed_are_refused_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(serve_backends, "available", lambda: {"cluster": "a", "other": "b"})
    with pytest.raises(ValueError, match="install exactly one"):
        serve_backends.resolve()


def test_listing_the_implementations_imports_none_of_them():
    mods = _imports_after("from robovast.service.serve_backends import available;"
                          " available()")
    for forbidden in ("kubernetes", "docker",
                      "robovast.execution.cluster_execution.cluster_service"):
        assert forbidden not in mods, f"listing pulled {forbidden}"


def test_the_shared_base_loads_no_driver():
    """``ServiceBase`` reaches for no driver: an implementation reaches its driver inside the
    hook that needs it."""
    mods = _imports_after("import robovast.service.service_base")
    for forbidden in ("kubernetes", "docker",
                      "robovast.execution.cluster_execution.cluster_service"):
        assert forbidden not in mods, f"the shared base pulled {forbidden}"


def test_the_conventional_port_has_one_definition():
    """The port clients probe and the port the service and its manifests use are one
    definition."""
    from robovast.execution.cluster_execution.service_deploy import SERVICE_PORT
    from robovast.service.app import DEFAULT_PORT as served
    from robovast.service.interface import DEFAULT_PORT as canonical

    assert canonical is served is SERVICE_PORT


def test_finding_a_local_service_does_not_touch_the_cluster_package():
    """`detected_service_url` is the first thing any client does, so it must not import
    the cluster deploy module to learn which port to probe."""
    mods = _imports_after(
        "from robovast.client.service_target import detected_service_url;"
        " detected_service_url()")
    assert not [m for m in mods if "cluster" in m], "the client pulled cluster code"
