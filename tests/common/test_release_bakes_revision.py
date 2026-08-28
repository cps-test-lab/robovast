# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A locally released image must be able to say which revision it was built from.

The failure this exists for: ``make release-images PROJECT=<registry> PUSH=1`` followed by
``vast service upgrade`` deployed a service whose ``code_revision`` was empty, because
only CI passed ``ROBOVAST_GIT_REVISION`` and the build scripts did not. ``get_service_info``
then reported no revision at all, so "is the change I just made loaded?" -- the one question
that field exists to answer -- could only be answered by probing for the behaviour.

The value is computed twice, in two languages: ``git`` in ``container/git_revision.sh`` for
the build, and :func:`robovast.common.execution.code_revision` for the process. The whole
feature is a *string comparison* between those two, so the test that matters runs the shell
helper and asserts equality with the Python one. A regex over the two sources would pass
while they diverged, which is the same as having no test.
"""

import subprocess
from pathlib import Path

import pytest

from robovast.common.execution import GIT_REVISION_ENV, code_revision

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "container" / "git_revision.sh"
#: Images whose Dockerfile declares the ARG are the ones a build has to bake. Discovered
#: rather than listed, so a new image with the ARG and no bake fails here instead of
#: shipping a deployment that cannot report itself.
WITH_ARG = sorted(path for path in (ROOT / "container").rglob("Dockerfile*")
                  if f"ARG {GIT_REVISION_ENV}" in path.read_text())


def _baked_by_the_helper() -> str:
    """The value ``container/git_revision.sh`` would pass, for this checkout."""
    script = (f'. "{HELPER}"; git_revision_args "{ROOT}"; '
              'printf "VALUE=%s\\n" "${GIT_REVISION_ARGS[1]:-}"')
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         check=True, timeout=30).stdout
    line = [ln for ln in out.splitlines() if ln.startswith("VALUE=")]
    assert line, f"helper printed nothing to read a value from:\n{out}"
    return line[-1][len("VALUE="):]


def test_the_helper_bakes_exactly_what_the_code_reports(monkeypatch):
    """The one that matters. Both sides read the same checkout, so they must agree
    character for character -- ``+dirty`` included, since a dirty tree is the normal
    research loop and is exactly when this comparison gets used."""
    # Both sides must answer from git, not from an already-baked value inherited from the
    # environment -- that would compare the env var with itself.
    monkeypatch.delenv(GIT_REVISION_ENV, raising=False)
    expected = code_revision()
    baked = _baked_by_the_helper()
    if not expected:
        assert baked == "", ("the code cannot determine a revision here, so the helper must "
                             "not invent one -- an empty answer is what 'this deployment "
                             f"cannot tell you' is made of, and it baked {baked!r}")
    else:
        assert baked == f"{GIT_REVISION_ENV}={expected}"


def test_the_helper_names_the_variable_the_code_reads():
    """A rename on the Python side that the shell does not follow bakes into a variable
    nothing reads, and the symptom is again a service that cannot report itself."""
    assert GIT_REVISION_ENV in HELPER.read_text()


def test_there_are_images_to_check():
    """Guards the discovery above: if the ARG is renamed everywhere, the parametrized test
    below would silently cover nothing."""
    assert WITH_ARG, f"no Dockerfile under container/ declares 'ARG {GIT_REVISION_ENV}'"


@pytest.mark.parametrize("dockerfile", WITH_ARG, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_image_declaring_the_arg_is_built_with_it(dockerfile):
    """Declaring the ARG and never passing it is the exact shape of the original bug: the
    Dockerfile looks like it records a revision, and every image built outside CI records
    an empty string."""
    candidates = [dockerfile.parent / "build.sh", ROOT / "container" / "release_images.sh"]
    bakers = [path for path in candidates if path.exists()
              and "git_revision.sh" in path.read_text()
              and "GIT_REVISION_ARGS" in path.read_text()]
    assert bakers, (
        f"{dockerfile.relative_to(ROOT)} declares 'ARG {GIT_REVISION_ENV}' but neither "
        f"{candidates[0].relative_to(ROOT)} nor container/release_images.sh sources "
        "container/git_revision.sh and passes GIT_REVISION_ARGS. Wire it in, or an image "
        "built outside CI reports no revision.")
