# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The image family must name images CI actually publishes.

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

from robovast.common.execution import (FAMILY_MEMBERS, FLOATING_IMAGE_TAG, MEMBER_ROBOVAST,
                                       MEMBER_ROQSIM, default_image_tag, resolve_family_image)

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "image.yml"

#: Each family member with the ``IMAGE_NAME`` suffix its CI job publishes under.
#: ``robovast`` itself is the bare name, hence the empty suffix.
CI_SUFFIXES = {member: member[len(MEMBER_ROBOVAST):] for member in FAMILY_MEMBERS}


@pytest.mark.parametrize("member", sorted(FAMILY_MEMBERS))
def test_every_member_is_published_by_ci(member):
    """A member nobody builds is a default that 404s at pull time, on a node."""
    workflow = WORKFLOW.read_text()
    assert f"${{{{ env.IMAGE_NAME }}}}{CI_SUFFIXES[member]}" in workflow, (
        f"no CI job publishes {member}; the family names an image nobody builds")


def test_the_default_tag_is_one_ci_publishes_on_every_merge(monkeypatch):
    """`latest` is the only unconditional tag, so it is the only possible default.

    Regression: the default was briefly derived from the installed version (``2.0``),
    which reads well and is wrong — ``type=semver`` tags are produced only for ``v*``
    pushes, and this project is at 2.0.0 with no ``v2`` tag ever pushed. The derived
    default named an image that does not exist, which is the very failure the rest of
    this module exists to catch.
    """
    monkeypatch.delenv("ROBOVAST_PROJECT_TAG", raising=False)
    assert default_image_tag() == FLOATING_IMAGE_TAG
    workflow = WORKFLOW.read_text()
    assert "type=raw,value=latest,enable={{is_default_branch}}" in workflow


def test_a_pinned_tag_wins_over_the_default(monkeypatch):
    monkeypatch.setenv("ROBOVAST_PROJECT", "ghcr.io/example-org")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "dev")
    assert resolve_family_image(f"family:{MEMBER_ROQSIM}") == \
        f"ghcr.io/example-org/{MEMBER_ROQSIM}:dev"


@pytest.mark.parametrize("mode", ["base", "ros2"])
def test_both_roqsim_shapes_name_the_combined_member(mode):
    """One member for both shapes — the only image with roqsim *and* the contract.

    Regression on two counts: the ROS shape used to default to roqsim's own published
    image, which carries the simulator but not ``/etc/robovast_compat_version``, so the
    runner rejected it — and nothing published that tag anyway.

    Asserted on what ``containers()`` returns rather than on a module constant: the
    constant is not the contract, the contributed container block is.
    """
    try:
        from robovast_sim_roqsim.backend import RoqsimBackend, RoqsimConfig
    except ModuleNotFoundError as exc:
        pytest.skip(f"robovast_sim_roqsim is not installed ({exc}); "
                    "run 'poetry install' to check this")
    blocks = RoqsimBackend().containers(
        RoqsimConfig(config="roqsim_scenes:depot"), {"mode": mode})
    images = {block["image"] for block in blocks.values() if block.get("image")}
    assert images == {f"family:{MEMBER_ROQSIM}"}, (
        f"mode {mode} contributed {images}, not the combined member")
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


#: A registry host in an image ref: the part before the first ``/``, recognised by having a
#: dot (a bare ``ubuntu:24.04`` names no registry and Docker Hub resolves it).
IMAGE_REGISTRY_RE = re.compile(
    r"\bimage:\s*[\"']?([a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?)/", re.IGNORECASE)

#: The only registries a shipped example may name, plus the RFC 2606 placeholder domains a
#: doc example should use.
#:
#: An ALLOWLIST rather than a denylist of the site's own hosts, and that is the point. This
#: guarded one literal host; when that host was retired for its successor the test still
#: passed while pointing at a dead name, so it was widened to a pattern for that site's
#: domain -- which guarded one *site*, and put that site's internal naming into a public
#: repository to do it. The property has nothing to do with any particular site: an example
#: must be runnable by whoever cloned the repo, so it may only name a registry they can
#: actually reach. Inverting it also means a new site's harbor is caught the first time,
#: with no pattern to extend, and this file names no infrastructure at all.
PUBLIC_REGISTRIES = frozenset({
    "docker.io", "index.docker.io", "registry-1.docker.io",
    "ghcr.io", "quay.io", "gcr.io", "public.ecr.aws", "registry.k8s.io",
    "example.com", "example.org", "example.net",
})


def test_no_shipped_example_pins_an_unreachable_registry():
    """A shipped example must be runnable by anyone who clones the repo.

    The first offender pinned a site-internal harbor host by digest, so the example only
    ran at the one site that could resolve it.
    """
    examples = Path(__file__).resolve().parents[2] / "configs" / "examples"
    offenders = {}
    for path in examples.rglob("*.vast"):
        bad = sorted({
            host for host in IMAGE_REGISTRY_RE.findall(path.read_text())
            if host.lower() not in PUBLIC_REGISTRIES
            and not host.lower().endswith((".example.com", ".example.org", ".example.net"))
        })
        if bad:
            offenders[str(path.relative_to(examples))] = bad
    assert not offenders, (
        f"examples name a registry a fresh clone cannot reach: {offenders} — "
        f"use one of {sorted(PUBLIC_REGISTRIES)} or drop the image and let the family resolve it")


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

    Both spellings are collected, because the pin moved: it used to be a literal
    ``git checkout <sha>``, and is now an ``ARG SCENARIO_EXECUTION_REF``. Matching only the
    old one left this test passing on the *other* repo's checkout while guarding nothing.
    """
    dockerfile = (WORKFLOW.parents[2] / "container" / "robovast" / "Dockerfile").read_text()
    pins = (re.findall(r"git checkout ([0-9a-f]{6,})", dockerfile)
            + re.findall(r"^ARG \w*_REF=([0-9a-f]{6,})", dockerfile, re.MULTILINE))
    assert len(pins) >= 2, (
        f"expected a pin for scenario-execution AND scenario-execution-server, found {pins}")
    for sha in pins:
        assert len(sha) == 40, f"{sha} is not a full commit sha"


def test_the_dockerfiles_build_without_a_staged_context():
    """Every source a Dockerfile needs must be something it can fetch itself.

    Both images used to ``COPY`` a directory that ``build.sh`` created *empty* in a temp
    context — and git cannot track an empty directory, so with CI's ``context: .`` the COPY
    had nothing to resolve against. Neither image's workflow could ever have succeeded,
    which is why nothing published the images the built-in defaults named.
    """
    container = WORKFLOW.parents[2] / "container" / "robovast"
    for name in ("Dockerfile", "Dockerfile.roqsim"):
        body = (container / name).read_text()
        for line in body.splitlines():
            if not line.startswith("COPY "):
                continue
            assert "--from=" in line, (
                f"{name}: {line.strip()!r} copies from the build context, which means the "
                "image cannot be built without a wrapper staging that path first")
