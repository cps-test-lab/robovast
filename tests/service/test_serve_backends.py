# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast serve`` resolves its execution lane instead of importing one.

The core registers no lane: reaching directly into the cluster service would mean the
core could not be installed without the cluster code, and an install with no Kubernetes
at all would die on an import of a module the user never named, which reads as broken
rather than absent.

Two properties carry that, and both are easy to lose by accident:

* **Listing is free.** ``available()`` is what lets a caller say "no lane is installed"
  politely. If it imported the lanes to list them, asking the question would cost the
  answer — and on a machine without a kubeconfig, raise instead of reporting.
* **The base loads no lane.** ``ServiceBase`` is imported wherever a lane is, and reaches
  for no driver of its own.
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


def test_the_cluster_lane_is_registered():
    assert "cluster" in serve_backends.available()


def test_the_only_installed_lane_is_the_default():
    """With one lane installed nothing has to be named: the service runs it."""
    name, lane = serve_backends.resolve()
    assert name == "cluster"
    assert isinstance(lane, serve_backends.ServeBackend)
    assert lane.storage


def test_a_registered_lane_resolves_by_name():
    name, lane = serve_backends.resolve("cluster")
    assert name == "cluster"
    assert isinstance(lane, serve_backends.ServeBackend)


def test_an_absent_lane_names_the_ones_that_exist():
    """The message is the feature: "not installed", not ModuleNotFoundError."""
    with pytest.raises(ValueError) as excinfo:
        serve_backends.resolve("gpu")
    message = str(excinfo.value)
    assert "no execution lane named 'gpu' is installed" in message
    assert "cluster" in message


def test_no_lane_at_all_names_the_distribution_that_ships_one(monkeypatch):
    """A core alone serves nothing, and says which package to install rather than
    starting a service that could run no campaign."""
    monkeypatch.setattr(serve_backends, "available", lambda: {})
    with pytest.raises(ValueError, match="robovast-cluster"):
        serve_backends.resolve()


def test_several_lanes_need_one_named(monkeypatch):
    monkeypatch.setattr(serve_backends, "available", lambda: {"cluster": "a", "other": "b"})
    with pytest.raises(ValueError, match="--backend"):
        serve_backends.resolve()


def test_listing_the_lanes_imports_none_of_them():
    mods = _imports_after("from robovast.service.serve_backends import available;"
                          " available()")
    for forbidden in ("kubernetes", "docker",
                      "robovast.execution.cluster_execution.cluster_service"):
        assert forbidden not in mods, f"listing pulled {forbidden}"


def test_the_shared_base_loads_no_lane_and_no_driver():
    """``ServiceBase`` is what a lane subclasses, so it is imported wherever one is. It
    must reach for no driver and import no lane: a lane reaches its driver inside the hook
    that needs it, and the base has no hook body to reach with."""
    mods = _imports_after("import robovast.service.service_base")
    for forbidden in ("kubernetes", "docker",
                      "robovast.execution.cluster_execution.cluster_service"):
        assert forbidden not in mods, f"the shared base pulled {forbidden}"


def test_the_conventional_port_has_one_definition():
    """8800 was declared twice -- in `service/app.py` and in the cluster deploy
    manifests -- and a client probing for a local service read the *cluster* one. One
    edit to either would have had clients probing a port nothing listens on, and the
    client reaching into an operator module for an integer is what made that possible.
    """
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
