# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The built-in image defaults must name images CI actually publishes.

A default that points at a tag no workflow produces is worse than no default: it
survives review, passes every unit test, and fails only when someone without an
explicit ``image:`` tries to run something. ``robovast-robosito`` defaulted to
``:jazzy`` for exactly that reason — CI publishes ``:latest``/branch/PR/semver and
has never produced ``:jazzy``.

So these tests read ``.github/workflows/image.yml`` and check the two against each
other, rather than asserting a string against itself.
"""

from pathlib import Path

import pytest

from robovast.common.execution import (DEFAULT_ROBOVAST_CONTROLLER_IMAGE,
                                       DEFAULT_ROBOVAST_IMAGE,
                                       DEFAULT_ROBOVAST_SIDECAR_IMAGE)

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "image.yml"

#: Every built-in default, with the ``IMAGE_NAME`` suffix its CI job publishes under.
#: ``robovast`` itself is the bare name, hence the empty suffix.
DEFAULTS = {
    "": DEFAULT_ROBOVAST_IMAGE,
    "-controller": DEFAULT_ROBOVAST_CONTROLLER_IMAGE,
    "-sidecar": DEFAULT_ROBOVAST_SIDECAR_IMAGE,
}


def _combined_image():
    """The robosito default, imported lazily — it lives in a separate distribution."""
    from robovast_sim_robosito.backend import DEFAULT_COMBINED_IMAGE
    return DEFAULT_COMBINED_IMAGE


@pytest.mark.parametrize("suffix,default", sorted(DEFAULTS.items()))
def test_default_repository_is_published_by_ci(suffix, default):
    workflow = WORKFLOW.read_text()
    repo = default.rsplit(":", 1)[0]
    assert repo.startswith("ghcr.io/cps-test-lab/robovast"), (
        f"{default} is not under the org's GHCR namespace")
    assert f"${{{{ env.IMAGE_NAME }}}}{suffix}" in workflow, (
        f"no CI job publishes {repo}; the default names an image nobody builds")


@pytest.mark.parametrize("default", sorted(DEFAULTS.values()))
def test_defaults_use_a_tag_ci_produces(default):
    # `latest` is the only tag published unconditionally on the default branch
    # (`type=raw,value=latest,enable={{is_default_branch}}`); branch, PR and semver
    # tags all depend on the ref, so none of them can be a built-in default.
    assert default.endswith(":latest"), (
        f"{default} pins a tag CI does not publish for every default-branch build")


def test_robosito_default_matches_its_ci_tag():
    """Regression: this default was ``:jazzy``, which no workflow has ever produced."""
    combined = _combined_image()
    assert combined == "ghcr.io/cps-test-lab/robovast-robosito:latest", combined
    assert "${{ env.IMAGE_NAME }}-robosito" in WORKFLOW.read_text()


def test_no_shipped_example_pins_a_private_registry():
    """A shipped example must be runnable by anyone who clones the repo.

    ``basic_nav_rst.vast`` pinned ``harbor.example.org`` by digest, so the example
    only ran at one site.
    """
    examples = Path(__file__).resolve().parents[2] / "configs" / "examples"
    offenders = [
        path.relative_to(examples)
        for path in examples.rglob("*.vast")
        if "harbor.example.org" in path.read_text()
    ]
    assert not offenders, f"examples pin an unreachable private registry: {offenders}"
