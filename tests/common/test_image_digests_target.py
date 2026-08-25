# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``make image-digests`` must report on the images the code actually resolves.

The target's value is that its answer can be trusted unread: "this project is a complete,
pullable set". A member name or variable renamed on one side and not the other produces
output that looks right and describes nothing -- leaving the operator believing a
deployment is ready when the next thing to touch it is a pod in ImagePullBackOff.

So the script is checked against ``robovast.common.execution`` and against
``release_images.sh``, rather than against a copy of itself.
"""

import re
from pathlib import Path

import pytest

from robovast.common.execution import FAMILY_MEMBERS

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "container" / "image_digests.sh"
MAKEFILE = ROOT / "Makefile"
#: The readers, plural on purpose: the simulator image is named in its own distribution
#: (robovast stays standalone and ships no simulator), so a single-file check would call a
#: variable unread when it is merely read elsewhere.
READERS = (ROOT / "src" / "robovast" / "common" / "execution.py",
           ROOT / "src" / "robovast_sim_roqsim" / "robovast_sim_roqsim" / "backend.py")


def _emitted_members():
    """The family members the script declares it will report on."""
    match = re.search(r"declare -a MEMBERS=\(([^)]*)\)", SCRIPT.read_text())
    assert match, "MEMBERS array not found in image_digests.sh"
    return match.group(1).split()


def _emitted_vars():
    """The variable names the script prints as configuration."""
    return re.findall(r'^echo "(ROBOVAST_[A-Z_]+)=', SCRIPT.read_text(), re.MULTILINE)


def test_it_reports_on_exactly_the_family():
    """A member missing here is one whose absence from a registry goes unnoticed."""
    assert _emitted_members() == list(FAMILY_MEMBERS)


@pytest.mark.parametrize("var", _emitted_vars())
def test_every_emitted_variable_is_one_the_code_reads(var):
    read_by = [path.name for path in READERS if path.exists() and var in path.read_text()]
    assert read_by, (
        f"image_digests.sh prints {var}=..., which none of "
        f"{[p.name for p in READERS]} reads — pasting it into a config would change nothing")


def test_the_member_names_match_what_release_images_publishes():
    """The two scripts must name the same images, or the report describes nothing."""
    release = (ROOT / "container" / "release_images.sh").read_text()
    for member in _emitted_members():
        assert f"{member}:" in release, (
            f"image_digests.sh reports {member}, which release_images.sh does not build")


def test_the_target_refuses_without_a_project():
    """A default namespace would report someone else's images as if they were yours."""
    makefile = MAKEFILE.read_text()
    target = makefile.split("image-digests:", 1)[1].split("\n.PHONY", 1)[0]
    assert 'test -n "$(PROJECT)"' in target
    assert "exit 1" in target


def test_it_builds_nothing():
    """The point is that an operator with no local images can run it.

    Calling a build script here would make the target useless on the machine that needs
    it most -- a fresh checkout bringing up a second cluster.
    """
    body = SCRIPT.read_text()
    # "docker build" with a trailing space, so it does not match "docker buildx
    # imagetools" -- which is a registry read, and the very thing this must keep.
    for builder in ("build.sh", "docker build ", "buildx build"):
        assert builder not in body, f"image_digests.sh must not invoke {builder!r}"
    assert "imagetools inspect" in body, "it must read the registry, not the local daemon"
