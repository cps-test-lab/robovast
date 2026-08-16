# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast serve`` resolves its execution lane instead of importing one.

Reaching directly into ``robovast.service.cluster_service`` meant the core could not be
installed without the cluster code: an install with no Kubernetes at all would have died
on an import of a module the user never named, which reads as broken rather than absent.

Two properties carry that, and both are easy to lose by accident:

* **Listing is free.** ``available()`` is what lets a caller say "cluster is not
  installed" politely. If it imported the lanes to list them, asking the question would
  cost the answer — and on a machine without a kubeconfig, raise instead of reporting.
* **Choosing one does not load the other.** The whole point of separating them.
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


def test_both_lanes_are_registered():
    assert {"local", "cluster"} <= set(serve_backends.available())


@pytest.mark.parametrize("name", ["local", "cluster"])
def test_a_registered_lane_resolves_to_something_buildable(name):
    lane = serve_backends.resolve(name)
    assert isinstance(lane, serve_backends.ServeBackend)
    assert lane.storage


def test_an_absent_lane_names_the_ones_that_exist():
    """The message is the feature: "not installed", not ModuleNotFoundError."""
    with pytest.raises(ValueError) as excinfo:
        serve_backends.resolve("gpu")
    message = str(excinfo.value)
    assert "no execution lane named 'gpu' is installed" in message
    assert "cluster" in message and "local" in message


def test_listing_the_lanes_imports_none_of_them():
    mods = _imports_after("from robovast.service.serve_backends import available;"
                          " available()")
    for forbidden in ("kubernetes", "docker", "robovast.service.cluster_service",
                      "robovast.service.local_transport"):
        assert forbidden not in mods, f"listing pulled {forbidden}"


def test_choosing_the_local_lane_does_not_load_the_cluster_one():
    mods = _imports_after("from robovast.service.serve_backends import resolve;"
                          " resolve('local')")
    assert "robovast.service.cluster_service" not in mods
    assert "kubernetes" not in mods


def test_choosing_the_cluster_lane_does_not_load_the_local_transport():
    """Symmetry matters once the lanes are separate packages: the in-pod service has no
    Docker, and should not import 3,000 lines of local lane to find that out."""
    mods = _imports_after("from robovast.service.serve_backends import resolve;"
                          " resolve('cluster')")
    assert "robovast.service.local_transport" not in mods
