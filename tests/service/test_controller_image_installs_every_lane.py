# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The controller image must install every distribution the in-pod service needs.

This is the one packaging mistake that cannot fail in CI. The lanes are separate
distributions that *depend on* robovast rather than being extras of it, so they cannot
ride the ``--extras`` string and are easy to leave out of a Dockerfile that looks
complete. A pod built that way starts, serves the API and the UI, answers ``/healthz`` --
and then fails every ``create_campaign`` for want of an execution lane. Healthy-looking
and able to run nothing.

The check is deliberately textual. Building the image needs Docker, a registry and
minutes; reading its install steps needs neither and catches the same omission at the
point it is introduced.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

import pathlib
import re

import pytest

DOCKERFILE = (pathlib.Path(__file__).resolve().parents[2]
              / "container" / "controller" / "Dockerfile")

#: Distributions whose absence from the pod is a silent runtime failure rather than an
#: import error at startup -- they contribute entry points, not imports.
REQUIRED_LOCAL_PACKAGES = ["robovast_cluster"]


@pytest.fixture(scope="module")
def dockerfile() -> str:
    assert DOCKERFILE.is_file(), f"{DOCKERFILE} moved; this guard now checks nothing"
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.mark.parametrize("package", REQUIRED_LOCAL_PACKAGES)
def test_the_image_installs_the_package(dockerfile, package):
    """Not merely mentioned in a comment -- installed by a RUN line."""
    installed = re.search(
        rf"(pip|poetry) install[^\n]*\b{re.escape(package)}\b", dockerfile)
    assert installed, (
        f"{package} is never installed in the controller image. The pod would come up "
        f"with no execution lane and fail every create_campaign.")


def test_the_lane_is_not_smuggled_in_as_an_extra(dockerfile):
    """`--extras "... cluster"` would silently do nothing: extras name optional
    dependencies of robovast, and the lane is not one of them. It would read as
    installed while the pod had no lane at all -- worse than omitting it, because the
    Dockerfile would look correct."""
    for line in dockerfile.splitlines():
        match = re.search(r'--extras\s+"([^"]*)"', line)
        if match:
            assert "cluster" not in match.group(1).split(), (
                "the cluster lane is a distribution, not an extra of robovast; "
                "--extras cannot install it")


def test_the_declared_extras_all_exist(dockerfile):
    """A typo in the extras string is accepted silently by poetry, so the pod loses a
    capability with no error anywhere. Check the names against robovast's own manifest.
    """
    import tomllib  # pylint: disable=import-outside-toplevel

    manifest = DOCKERFILE.parents[2] / "pyproject.toml"
    declared = set(tomllib.loads(manifest.read_text(encoding="utf-8"))
                   ["tool"]["poetry"]["extras"])

    used = set()
    for line in dockerfile.splitlines():
        match = re.search(r'--extras\s+"([^"]*)"', line)
        if match:
            used.update(match.group(1).split())

    assert used, "no --extras found; this guard is checking nothing"
    assert used <= declared, f"undeclared extras in the Dockerfile: {sorted(used - declared)}"
