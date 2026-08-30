# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A locally released image must be able to say which revision it was built from, and when.

The failure this exists for: ``make release-images PROJECT=<registry> PUSH=1`` followed by
``vast service upgrade`` deployed a service whose ``code_revision`` was empty, because
only CI passed ``ROBOVAST_GIT_REVISION`` and the build scripts did not. ``get_service_info``
then reported no revision at all, so "is the change I just made loaded?" -- the one question
that field exists to answer -- could only be answered by probing for the behaviour.

The build date is baked through the same helper for the same reason, and is covered here
rather than in a file of its own precisely because a second stamp wired into CI and not into
the local build scripts is that same bug a second time.

The revision is computed twice, in two languages: ``git`` in ``container/image_stamp.sh`` for
the build, and :func:`robovast.common.execution.code_revision` for the process. The whole
feature is a *string comparison* between those two, so the test that matters runs the shell
helper and asserts equality with the Python one. A regex over the two sources would pass
while they diverged, which is the same as having no test.
"""

import re
import subprocess
from pathlib import Path

import pytest

from robovast.common.execution import BUILD_DATE_ENV, GIT_REVISION_ENV, build_date, code_revision

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "container" / "image_stamp.sh"

#: The two stamps, each as ``(env var, the array the helper sets)``. Parametrized rather than
#: written twice so a third stamp costs one line here and is then held to the same rules.
STAMPS = [(GIT_REVISION_ENV, "GIT_REVISION_ARGS"), (BUILD_DATE_ENV, "BUILD_DATE_ARGS")]


def _baked_by_the_helper(array: str) -> str:
    """The value ``container/image_stamp.sh`` would pass in *array*, for this checkout."""
    script = (f'. "{HELPER}"; image_stamp_args "{ROOT}"; '
              f'printf "VALUE=%s\\n" "${{{array}[1]:-}}"')
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
    baked = _baked_by_the_helper("GIT_REVISION_ARGS")
    if not expected:
        assert baked == "", ("the code cannot determine a revision here, so the helper must "
                             "not invent one -- an empty answer is what 'this deployment "
                             f"cannot tell you' is made of, and it baked {baked!r}")
    else:
        assert baked == f"{GIT_REVISION_ENV}={expected}"


def test_the_helper_bakes_a_date_the_code_can_read_back():
    """No Python counterpart to compare against -- ``build_date`` only ever reads the env --
    so what has to hold is the format: RFC 3339 in UTC, matching what
    ``docker/metadata-action`` writes into ``org.opencontainers.image.created`` so a CI-built
    and a locally built image are read the same way. A local format drift would surface as a
    date the UI renders as ``Invalid Date`` on locally released images only."""
    baked = _baked_by_the_helper("BUILD_DATE_ARGS")
    assert baked.startswith(f"{BUILD_DATE_ENV}=")
    value = baked[len(BUILD_DATE_ENV) + 1:]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value), value


@pytest.mark.parametrize("env,array", STAMPS, ids=[env for env, _ in STAMPS])
def test_the_helper_names_the_variable_the_code_reads(env, array):
    """A rename on the Python side that the shell does not follow bakes into a variable
    nothing reads, and the symptom is again a service that cannot report itself."""
    text = HELPER.read_text()
    assert env in text
    assert array in text


@pytest.mark.parametrize("env,_array", STAMPS, ids=[env for env, _ in STAMPS])
def test_there_are_images_to_check(env, _array):
    """Guards the discovery below: if an ARG is renamed everywhere, the parametrized test
    after it would silently cover nothing."""
    assert _images_declaring(env), f"no Dockerfile under container/ declares 'ARG {env}'"


def _images_declaring(env: str) -> "list[Path]":
    """Dockerfiles declaring ``ARG <env>``.

    Discovered rather than listed, so a new image with the ARG and no bake fails here
    instead of shipping a deployment that cannot report itself.
    """
    return sorted(path for path in (ROOT / "container").rglob("Dockerfile*")
                  if f"ARG {env}" in path.read_text())


@pytest.mark.parametrize("env,array", STAMPS, ids=[env for env, _ in STAMPS])
def test_every_image_declaring_the_arg_is_built_with_it(env, array):
    """Declaring the ARG and never passing it is the exact shape of the original bug: the
    Dockerfile looks like it records a stamp, and every image built outside CI records an
    empty string."""
    for dockerfile in _images_declaring(env):
        candidates = [dockerfile.parent / "build.sh", ROOT / "container" / "release_images.sh"]
        bakers = [path for path in candidates if path.exists()
                  and "image_stamp.sh" in path.read_text()
                  and array in path.read_text()]
        assert bakers, (
            f"{dockerfile.relative_to(ROOT)} declares 'ARG {env}' but neither "
            f"{candidates[0].relative_to(ROOT)} nor container/release_images.sh sources "
            f"container/image_stamp.sh and passes {array}. Wire it in, or an image "
            "built outside CI reports nothing for it.")


def test_a_build_date_is_never_invented(monkeypatch):
    """The counterpart to the revision's rule, and the reason it matters more here: a
    revision that is absent is obviously absent, while a substituted date -- today, a file
    mtime -- looks exactly like a real answer and would be read as the age of the
    deployment."""
    monkeypatch.setenv(BUILD_DATE_ENV, "")
    assert build_date() == ""
    monkeypatch.setenv(BUILD_DATE_ENV, "   ")
    assert build_date() == ""
    monkeypatch.setenv(BUILD_DATE_ENV, "2026-08-12T09:41:00Z")
    assert build_date() == "2026-08-12T09:41:00Z"
