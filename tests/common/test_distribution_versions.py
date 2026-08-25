# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The distributions released as one RoboVAST version must not drift apart.

A version is not an identity here -- provenance is a git revision, and
``test_a_package_version_is_not_an_identity`` refuses a campaign identified by a semver.
The number exists for the one job a commit cannot do: naming a release on an index, which
is also what ``robovast = "^2.0.0"`` in the siblings resolves against. So it only has to
be right at the moment of publishing -- and the way it goes wrong is quietly, in one of
five files nobody was editing.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Released together, so one version covers them.
IN_STEP = (
    "pyproject.toml",
    "src/robovast_client/pyproject.toml",
    "src/robovast_nav/pyproject.toml",
    "src/robovast_cluster/pyproject.toml",
)

#: Versioned on its own, and able to be: it declares no dependency on ``robovast``, so
#: nothing ties it to a RoboVAST release. Listed rather than skipped, so a new
#: distribution has to be put in one column or the other on purpose.
INDEPENDENT = ("src/robovast_sim_roqsim/pyproject.toml",)


def _manifests():
    return sorted(p.relative_to(REPO).as_posix()
                  for p in [REPO / "pyproject.toml", *(REPO / "src").glob("*/pyproject.toml")])


def _version(rel):
    for line in (REPO / rel).read_text().splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise AssertionError(f"{rel} declares no version")


def test_every_distribution_is_accounted_for():
    """A sixth one must not default into either column by being forgotten."""
    assert set(_manifests()) == set(IN_STEP) | set(INDEPENDENT), (
        "a distribution is neither released in step nor listed as independent -- add it to "
        "IN_STEP or INDEPENDENT in this file, whichever it is")


def test_the_released_distributions_share_one_version():
    versions = {rel: _version(rel) for rel in IN_STEP}
    assert len(set(versions.values())) == 1, (
        f"these are published as one release and disagree: {versions}")


def test_a_sibling_constraint_admits_the_version_it_ships_with():
    """``robovast = "^2.0.0"`` is resolved from the index by a standalone install, so a
    major bump that lands in only one of the two files is an install that cannot resolve."""
    root_major = _version("pyproject.toml").split(".")[0]
    for rel in IN_STEP:
        text = (REPO / rel).read_text()
        for constraint in re.findall(r'^robovast = "\^([0-9.]+)"', text, re.M):
            assert constraint.split(".")[0] == root_major, (
                f"{rel} requires robovast ^{constraint}, but robovast is "
                f"{_version('pyproject.toml')}")
