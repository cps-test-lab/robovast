# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The built-in image defaults must name images CI actually publishes.

A default that points at a tag no workflow produces is worse than no default: it
survives review, passes every unit test, and fails only when someone without an
explicit ``image:`` tries to run something. ``robovast-roqsim`` defaulted to
``:jazzy`` for exactly that reason — CI publishes ``:latest``/branch/PR/semver and
has never produced ``:jazzy``.

So these tests read ``.github/workflows/image.yml`` and check the two against each
other, rather than asserting a string against itself.
"""

import re
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
    """The roqsim default, imported lazily — it lives in a separate distribution.

    Skipped rather than failed when that distribution is not installed: "the environment
    is incomplete" and "the default drifted from CI" are different findings, and
    reporting the first as the second sends the reader to the wrong file.
    """
    try:
        from robovast_sim_roqsim.backend import DEFAULT_COMBINED_IMAGE
    except ModuleNotFoundError as exc:
        pytest.skip(f"robovast_sim_roqsim is not installed ({exc}); "
                    "run 'poetry install' to check this default")
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


def test_roqsim_default_matches_its_ci_tag():
    """Regression: this default was ``:jazzy``, which no workflow has ever produced."""
    combined = _combined_image()
    assert combined == "ghcr.io/cps-test-lab/robovast-roqsim:latest", combined
    assert "${{ env.IMAGE_NAME }}-roqsim" in WORKFLOW.read_text()


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
    Written out twice it drifts, which is how the roqsim job ended up with no
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
    for name in ("PLATFORMS_ROBOVAST", "PLATFORMS_ROQSIM"):
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


#: Registry hosts a shipped example must never name. Matched as a pattern rather than a
#: literal: the first offender was ``harbor.example.org``, and when that host was retired
#: for ``harbor.example.org`` this test still passed while pointing at a dead name — it
#: guarded one spelling instead of the property. Anything under the site's private domain
#: is unreachable to someone who cloned the repo, whatever it is called this year.
PRIVATE_REGISTRY_RE = re.compile(r"\bharbor\.example\.[a-z0-9.-]+", re.IGNORECASE)


def test_no_shipped_example_pins_a_private_registry():
    """A shipped example must be runnable by anyone who clones the repo.

    ``basic_nav_roqsim.vast`` pinned ``harbor.example.org`` by digest, so the example
    only ran at one site.
    """
    examples = Path(__file__).resolve().parents[2] / "configs" / "examples"
    offenders = {
        str(path.relative_to(examples)): sorted(set(found))
        for path in examples.rglob("*.vast")
        if (found := PRIVATE_REGISTRY_RE.findall(path.read_text()))
    }
    assert not offenders, f"examples pin an unreachable private registry: {offenders}"


def test_a_push_publishes_only_the_prefixed_tag():
    """`buildx --push` publishes every -t it is given.

    The migration from `docker build` + `docker tag` + `docker push <prefixed>` to a
    single buildx call quietly changed this: tagging with the bare local name as well
    made the push try `library/robovast_jazzy` on Docker Hub and fail with "push access
    denied". Only a real registry could catch it, so pin the shape here.
    """
    script = (WORKFLOW.parents[2] / "container" / "robovast" / "build.sh").read_text()
    helper = script.split("buildx_args() {", 1)[1].split("\n}", 1)[0]
    push_branch = helper.split('if [[ -n "${PUSH:-}" ]]; then', 1)[1].split("elif", 1)[0]
    assert "published_tag" in push_branch
    assert "local_tag" not in push_branch, (
        "a --push build must not carry the bare local tag: buildx would publish it too")


def test_the_scenario_execution_pin_is_a_full_commit_sha():
    """A pin that is a branch name silently moves; one that is a short sha can collide.

    The stronger property — that the commit is on a *durable* ref — cannot be checked
    without the network, and it is the one that broke: the pin pointed at a commit that
    only ever existed on a feature branch, and every build from a clean clone failed
    once that branch was merged and deleted.
    """
    import re
    dockerfile = (WORKFLOW.parents[2] / "container" / "robovast" / "Dockerfile").read_text()
    checkouts = re.findall(r"git checkout ([0-9a-f]+)", dockerfile)
    assert checkouts, "no pinned commits found"
    for sha in checkouts:
        assert len(sha) == 40, f"{sha} is not a full commit sha"
