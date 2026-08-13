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


PLATFORMS_ENV = WORKFLOW.parents[2] / "container" / "platforms.env"


def _platform_policy():
    """``container/platforms.env`` as a dict, without sourcing a shell."""
    policy = {}
    for line in PLATFORMS_ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            policy[key] = value
    return policy


def test_every_build_job_reads_the_shared_platform_policy():
    """No job may hard-code `platforms:`; both builders read one file.

    The architecture rule has to hold in CI *and* in container/release_images.sh.
    Written out twice it drifts, which is how the robosito job ended up with no
    `platforms:` at all while its base image was multi-arch.
    """
    workflow = WORKFLOW.read_text()
    policy = _platform_policy()

    for line in workflow.splitlines():
        stripped = line.strip()
        if stripped.startswith("platforms:"):
            assert "steps.plat.outputs.platforms" in stripped, (
                f"hard-coded platforms in image.yml: {stripped!r} — "
                f"read it from container/platforms.env instead")

    read = {line.split("$PLATFORMS_")[1].split("\"")[0]
            for line in workflow.splitlines() if "$PLATFORMS_" in line}
    assert read, "no job reads container/platforms.env"
    for name in read:
        assert f"PLATFORMS_{name}" in policy, (
            f"image.yml reads PLATFORMS_{name}, which container/platforms.env "
            f"does not define (it has: {sorted(policy)})")


def test_cluster_only_images_are_single_arch():
    """The cluster is linux/amd64; a second architecture there is never pulled."""
    policy = _platform_policy()
    assert policy["CLUSTER_PLATFORM"] == "linux/amd64"
    for name in ("PLATFORMS_CONTROLLER", "PLATFORMS_SIDECAR"):
        assert policy[name] == policy["CLUSTER_PLATFORM"], (
            f"{name} builds for an architecture no cluster node runs")
    for name in ("PLATFORMS_ROBOVAST", "PLATFORMS_ROBOSITO"):
        assert policy["CLUSTER_PLATFORM"] in policy[name], (
            f"{name} must still cover the cluster's architecture")


def test_no_dockerfile_hardcodes_a_download_architecture():
    """A fixed linux-amd64 URL in a multi-arch image ships the wrong binary.

    Both the base image's `mc` and `fixuid` did, so every arm64 build carried x86
    executables.
    """
    container = WORKFLOW.parents[2] / "container"
    offenders = []
    for path in sorted(container.rglob("Dockerfile*")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            # Comments may name the architecture while explaining why it is not fixed.
            if line.lstrip().startswith("#"):
                continue
            if "linux-amd64" in line:
                offenders.append(f"{path.relative_to(container)}:{number}")
    assert not offenders, (
        f"Dockerfiles hard-code linux-amd64 downloads: {offenders} — use $TARGETARCH")


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
