# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``make image-digests`` must emit the variable names the code actually reads.

The target's whole value is that its output can be pasted into ``.env`` unread. A
variable renamed on one side and not the other produces lines that look right, paste
cleanly, and are silently ignored -- leaving the deployment on the floating ``:latest``
it was trying to escape, with nothing to indicate it.

So the names in the script are checked against the reader in
``robovast.common.execution`` rather than against a copy of themselves.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "container" / "image_digests.sh"
MAKEFILE = ROOT / "Makefile"
#: The readers, plural on purpose: the simulator image is resolved in its own
#: distribution (robovast stays standalone and ships no simulator), so a single-file
#: check would call ROBOVAST_ROQSIM_IMAGE unread when it is merely read elsewhere.
READERS = (ROOT / "src" / "robovast" / "common" / "execution.py",
           ROOT / "src" / "robovast_sim_roqsim" / "robovast_sim_roqsim" / "backend.py")


def _emitted_vars():
    """The variable names the script declares it will print."""
    match = re.search(r"declare -a VARS=\(([^)]*)\)", SCRIPT.read_text())
    assert match, "VARS array not found in image_digests.sh"
    return match.group(1).split()


def _emitted_repos():
    match = re.search(r"declare -a REPOS=\(([^)]*)\)", SCRIPT.read_text())
    assert match, "REPOS array not found in image_digests.sh"
    return re.findall(r'"([^"]+)"', match.group(1))


@pytest.mark.parametrize("var", _emitted_vars())
def test_every_emitted_variable_is_one_the_code_reads(var):
    read_by = [path.name for path in READERS if path.exists() and var in path.read_text()]
    assert read_by, (
        f"image_digests.sh prints {var}=..., which none of "
        f"{[p.name for p in READERS]} reads — pasting it into .env would change nothing")


def test_one_line_per_image_and_no_silent_gaps():
    """Three variables, three repositories: a mismatch would skip an image quietly."""
    assert len(_emitted_vars()) == len(_emitted_repos())


def test_the_repository_names_match_what_release_images_publishes():
    """The two scripts must name the same images, or the digests describe nothing.

    ``release_images.sh`` builds ``robovast_<distro>``, ``robovast-controller`` and
    ``robovast_roqsim_<distro>``; reporting digests for differently-named repositories
    would report on images nobody publishes.
    """
    release = (ROOT / "container" / "release_images.sh").read_text()
    for repo in _emitted_repos():
        # Both scripts interpolate the distro the same way, so compare the literal shape.
        assert repo in release, (
            f"image_digests.sh reports {repo}, which release_images.sh does not build")


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
