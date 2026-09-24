# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

import copy
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from importlib.resources import files
from pprint import pformat

import yaml


# The node label is computed IN THE CONTAINER by ``execution/data/collect_sysinfo.py``,
# which is mounted into the image as a standalone script and can import nothing from this
# package. Re-exported through here so every host-side caller hashes identically: two
# definitions would let the label a pod wrote and the label a query looks for drift apart,
# and the join would go quietly empty rather than fail.
# pylint: disable=unused-import  # the re-export below is the documented import site
from robovast.execution.data.collect_sysinfo import node_label  # noqa: F401
from robovast.execution.campaign_archive import JOB_DOCUMENT_SUFFIXES

from .common import convert_dataclasses_to_dict, get_scenario_parameters
from .config import SIMULATION_CONTAINER
from .config_identifier import compute_config_identifier, hash_file_content, hash_run_files
from .sut_channel import SUT_CONFIG_FILE
from .sut_channel import source_paths as sut_source_paths
from .errors import CampaignConfigError, missing_input_error
from .simulators import SIM_CONFIG_FILE, SIM_OVERRIDES_MOUNT

# The host <-> container protocol: what host scripts assume about an image's entrypoint,
# paths and environment. Bump COMPAT_VERSION when that contract changes (a new required
# package, a ROS distro change, a script interface change). The same value must appear in the
# Dockerfiles as the org.robovast.compat-version label -- one marker, and the CI gate in
# .github/workflows/image.yml is what keeps it equal to this constant.
#
# A **window**, not a single value, and that is the whole point. This was compared with `!=`
# at every site, so the first bump orphaned every image already published: a campaign whose
# results pin an image by digest could never be re-run again, even with those exact bytes
# still sitting in the registry. Since re-running a year-old campaign is a thing robovast is
# supposed to support, equality was refusing the case it exists for.
#
# So the host declares the RANGE it can drive. Bumping the max is now cheap and harmless.
# Dropping support becomes a separate, deliberate act -- raising the min -- which is also
# where "we stopped speaking protocol N" gets recorded.
#
# The honesty requirement: the window is a CLAIM. Raise the min when support is genuinely
# dropped, or this replaces a safe refusal with a broken run. Equality was wrong for the use
# case, but it was wrong in the safe direction. configs/examples/camera_smoke is the cheap
# way to keep the claim true -- it runs a container and produces an artifact in seconds.
COMPAT_VERSION = 2

#: The oldest image protocol this host still knows how to drive. Equal to
#: :data:`COMPAT_VERSION` means "only the current one", which is where equality left us.
MIN_IMAGE_COMPAT = 2

#: Image label carrying the protocol version -- the only marker there is.
#:
#: It replaced a file inside the image for two reasons. Reading a file costs a `docker run`, a
#: whole container started to read one integer; and it cannot be read for a remote image at
#: all, which is the case the marker most needs to answer -- whether this host can still drive
#: the image a year-old campaign recorded, asked from a machine that does not have it.
#:
#: An image built before this label existed reports nothing, and `check_image_compat` refuses
#: rather than guessing. Those images predate protocol 2, which is also `MIN_IMAGE_COMPAT`, so a
#: refusal is the right answer for them anyway -- but the message has to say what to do about it,
#: not merely that it cannot tell.
COMPAT_VERSION_LABEL = "org.robovast.compat-version"

# The unprivileged user a robovast execution image runs as (fixuid is configured for it). Experiment
# image builds step up to root for apt/pip and must drop back to this, or a cluster pod -- which
# takes the user from the image, unlike a local run, where compose sets it explicitly -- would run
# the scenario as root.
DEFAULT_IMAGE_USER = "ubuntu:ubuntu"


# -- the RoboVAST image family ---------------------------------------------------------
#
# A family image ref glues three independent facts into one string. Authoring all three in
# one place turns image configuration into a five-variable problem:
#
#   harbor.example.org/robovast / robovast-roqsim : latest
#   \_______ WHERE ____________/   \____ WHAT ___/   \ WHICH /
#      deployment config             never a choice    version
#
# WHAT follows from the container's role and the campaign's mode, so it is never
# authored: a ``.vast`` names its OWN images and nothing else. That leaves one knob for
# WHERE (:envvar:`ROBOVAST_PROJECT`) and one for WHICH
# (:envvar:`ROBOVAST_PROJECT_TAG`), and the member is referred to symbolically until
# core resolves it -- exactly the treatment :data:`BUILD_IMAGE_PREFIX` gets below, and
# for the same reason: the author does not hold the context the ref needs.

#: Marks a ref that names a *member of the published set* rather than a concrete image:
#: ``family:<member>``. Resolved to ``<project>/<member>:<tag>`` by
#: :func:`resolve_family_image`; like a ``build:`` ref, one reaching a pod or compose
#: spec unresolved is a hard error rather than an image name nothing can pull.
FAMILY_IMAGE_PREFIX = "family:"


#: Where a built image records what it actually contains. Baked into the image rather than
#: written beside the campaign, because that is the only form that survives every path: every
#: pod gets it without extra plumbing, it travels with the image if the image is copied or
#: retagged, and a rebuild a year from now can read what the original installed and install
#: exactly that.
#:
#: Here rather than beside the build that writes it, because it is now read by two layers that
#: must not depend on each other -- the service's build code from a local image, and the
#: cluster's registry client from a layer blob. A second literal in either would be a path that
#: drifts silently, and the reader would simply find nothing.
BUILD_MANIFEST_DIR = "/etc/robovast/build-manifest"

#: Plain text, one record per line, rather than JSON. Both are produced by a shell `RUN`, where
#: emitting valid JSON means quoting hundreds of package names correctly and a mistake yields a
#: file that parses as something else; `pip freeze` output is also directly re-installable.
#:
#: Each is written by its own `RUN`, so each lands in its own layer -- which is why a reader
#: walking layers must keep going after the first hit instead of stopping at it.
BUILD_MANIFEST_FILES = ("apt.txt", "pip.txt", "vcs.txt")

#: The RoboVAST contract image: ROS, scenario-execution, the VNC stack, ``/out``, the
#: compat marker. What a campaign runs in when no simulator adds to it, and the ``FROM``
#: every experiment image builds on. Carries no ``robovast`` Python package.
MEMBER_ROBOVAST = "robovast"
#: ``MEMBER_ROBOVAST`` plus roqsim and MuJoCo. Used by **both** roqsim shapes: stepped
#: in-process (where the scenario container *is* the simulator) and the ROS shape (where
#: the simulator gets its own container) -- it is the only image carrying both roqsim and
#: the RoboVAST contract, so one member serves both roles.
MEMBER_ROQSIM = "robovast-roqsim"
#: The service/API/UI host (``vast serve``). Python only -- no ROS, no GL.
MEMBER_CONTROLLER = "robovast-controller"
#: The small alpine helper (``curl`` + GNU ``tar``) that moves a pod's bytes through the
#: service's data plane: the init container that lands a job's inputs, the container that
#: delivers its outputs, and the context fetch of an image build.
MEMBER_SIDECAR = "robovast-sidecar"

#: Every member of the published set. A member is a *repository* name under the project,
#: so the ROS distro is a tag and never part of the name -- ``container/robovast/build.sh``,
#: ``container/release_images.sh`` and ``.github/workflows/image.yml`` publish exactly
#: these four, and ``tests/common/test_image_defaults.py`` keeps them in agreement.
#: Enumerated so a typo fails here rather than as a 404 at pull time, on a node.
FAMILY_MEMBERS = (MEMBER_ROBOVAST, MEMBER_ROQSIM, MEMBER_CONTROLLER, MEMBER_SIDECAR)

#: Where the published set lives when :envvar:`ROBOVAST_PROJECT` says nothing.
DEFAULT_IMAGE_PROJECT = "ghcr.io/cps-test-lab"

#: The tag used when neither :envvar:`ROBOVAST_PROJECT_TAG` nor a release version applies.
#: A floating tag, so resolving to it warns -- see :func:`default_image_tag`.
FLOATING_IMAGE_TAG = "latest"

#: The branch CI publishes the family under on every push to it: ``image.yml`` builds pushes
#: to this branch alone and tags them ``type=ref,event=branch``, so the tag is its name.
DEFAULT_BRANCH_IMAGE_TAG = "main"

#: What ``type=ref,event=pr`` publishes for a pull request, moved by every push to it.
_PULL_REQUEST_IMAGE_TAG = re.compile(r"pr-\d+")


def is_floating_image_tag(reference: str) -> bool:
    """Whether CI moves this tag: ``latest``, the default branch's, or a pull request's.

    Any other reference -- a release version, a date, a digest -- was chosen for the
    deployment that names it, and pulling it again is meant to land on the same bytes.
    """
    return (reference in (FLOATING_IMAGE_TAG, DEFAULT_BRANCH_IMAGE_TAG)
            or _PULL_REQUEST_IMAGE_TAG.fullmatch(reference) is not None)


# Marks an image ref that is *produced by a build* rather than pulled: ``build:<name>``,
# where the name is the container whose packages produced it. Internal only -- no .vast
# writes one, and it is resolved to a concrete image before reaching a pod/compose spec
# by the build lifecycle (which knows the registry + digest). It survives as the guard
# that catches an unresolved ref reaching a container spec, where it would otherwise be
# used verbatim as an invalid image name.
BUILD_IMAGE_PREFIX = "build:"


#: How much can be said about where a container's image came from. A campaign is only
#: reproducible to the extent every one of its images can be identified, and these differ in
#: *why* they can be:
#:
#: 1  robovast built it, so the base, the resolved packages and the labels are all recorded.
#: 2  a member of the published family, or a `build:` ref -- ours, and carrying our labels.
#: 3  a user-supplied image with an authored ``provenance:`` block naming source and revision.
#: 4  a user-supplied image with nothing recorded anywhere. **Refused when authoring.**
IMAGE_TIER_BUILT = 1
IMAGE_TIER_FAMILY = 2
IMAGE_TIER_DECLARED = 3
IMAGE_TIER_OPAQUE = 4


def image_provenance_tier(name: str, block: dict) -> "tuple[int, str]":
    """``(tier, why)`` for one ``execution.containers`` entry.

    Deliberately **declarative only** -- it inspects the ``.vast`` and never the image. A check
    that read labels would answer differently depending on whether the image happened to be
    pulled locally, which is the wrong property for the collect-all validator the web editor and
    an agent both hit: the same file would validate on one machine and fail on another. An author
    who has relabelled their image can still declare the block, which costs two lines and always
    works.

    Tier 1 covers a container that adds packages *even if it also names an image*: that image is
    then the base robovast builds on, and the build records the base digest along with everything
    it installed.
    """
    block = block or {}
    image = (block.get("image") or "").strip()

    if (block.get("system_packages") or block.get("python_packages")
            or block.get("ros_packages")):
        return IMAGE_TIER_BUILT, "robovast builds this image, so its inputs are recorded"
    if not image:
        return IMAGE_TIER_FAMILY, "no image named; the backend or the role default supplies one"
    if image.startswith(FAMILY_IMAGE_PREFIX) or is_build_image_ref(image):
        return IMAGE_TIER_FAMILY, f"{image!r} is a robovast-published reference"
    if names_family_member(image):
        # A family member spelled out concretely rather than symbolically. Every real campaign in
        # this tree predates `family:` and writes `ghcr.io/<project>/robovast:latest`, and those
        # are OUR images -- they carry our labels and their build is in this repo. Reading only
        # the symbolic form would refuse the very images the tiering exists to bless, which is how
        # this was caught: the migration fixtures were flagged.
        return IMAGE_TIER_FAMILY, f"{image!r} names a published robovast family member"
    if block.get("provenance"):
        return IMAGE_TIER_DECLARED, f"{image!r} is user-supplied with declared provenance"
    return IMAGE_TIER_OPAQUE, (
        f"container {name!r} runs {image!r}, an image robovast neither built nor publishes, and "
        f"declares no 'provenance:'. Nothing in this campaign's results would be able to say "
        f"what that image was, so it could never be re-run or reproduced -- and the gap only "
        f"surfaces once it is too late to ask.\n"
        f"  Add, under execution.containers.{name}:\n"
        f"      provenance:\n"
        f"        source: <repo URL or path holding the image's build definition>\n"
        f"        revision: <the commit that built it>\n"
        f"        build_recipe: <optional: how, if it is not obvious>\n"
        f"  Or let robovast build it instead -- declare "
        f"'system_packages'/'python_packages'/'ros_packages' and "
        f"drop the image -- which records everything automatically.")


def names_family_member(image: str) -> bool:
    """Whether *image* is a concrete reference to a member of the published family.

    Matched on the repository *name* rather than the project, because the project is
    deliberately configurable (:envvar:`ROBOVAST_PROJECT`) -- an operator publishing the same
    family to their own registry is still running our images, built from this repo, carrying our
    labels. Keying on ``ghcr.io/cps-test-lab`` would have blessed only the default deployment.
    """
    ref = (image or "").strip()
    if not ref:
        return False
    # Strip a digest, then a tag, then take the last path segment: `<project>/<member>` is the
    # shape, and only the member identifies what the image *is*.
    ref = ref.split("@", 1)[0]
    head, _, tail = ref.rpartition("/")
    repository = (tail or head).split(":", 1)[0]
    return repository in FAMILY_MEMBERS


def opaque_image_containers(execution: dict) -> "list[tuple[str, str]]":
    """``[(container, why)]`` for every container whose image cannot be identified.

    One place, so the collect-all validator and the launch path cannot disagree about what counts
    -- a config that validated and then would not launch is worse than either answer alone.
    """
    containers = (execution or {}).get("containers") or {}
    if not isinstance(containers, dict):
        return []
    out = []
    for name, block in sorted(containers.items()):
        tier, why = image_provenance_tier(name, block if isinstance(block, dict) else {})
        if tier == IMAGE_TIER_OPAQUE:
            out.append((name, why))
    return out


def is_build_image_ref(image: str | None) -> bool:
    """True if *image* is a symbolic ``build:<tag>`` ref (an unresolved build)."""
    return bool(image) and image.strip().startswith(BUILD_IMAGE_PREFIX)


def build_image_tag(image: str) -> str:
    """The bare ``<tag>`` from a ``build:<tag>`` symbolic ref."""
    return image.strip()[len(BUILD_IMAGE_PREFIX):]


def family_image_ref(member: str) -> str:
    """The symbolic ``family:<member>`` ref for *member*."""
    if member not in FAMILY_MEMBERS:
        raise ValueError(
            f"unknown RoboVAST image family member {member!r}; "
            f"expected one of: {', '.join(FAMILY_MEMBERS)}")
    return FAMILY_IMAGE_PREFIX + member


def is_family_image_ref(image: str | None) -> bool:
    """True if *image* is a symbolic ``family:<member>`` ref."""
    return bool(image) and image.strip().startswith(FAMILY_IMAGE_PREFIX)


def family_member(image: str) -> str:
    """The ``<member>`` from a ``family:<member>`` ref, validated against the set."""
    member = image.strip()[len(FAMILY_IMAGE_PREFIX):]
    if member not in FAMILY_MEMBERS:
        raise CampaignConfigError(
            f"unknown RoboVAST image family member {member!r} in {image!r}; "
            f"expected one of: {', '.join(FAMILY_MEMBERS)}")
    return member


def default_image_project() -> str:
    """The project (registry/namespace) the image family is pulled from.

    ``ROBOVAST_PROJECT`` is the *one* image knob: it moves the whole set at once, which
    is why there are no per-image variables: every variable has to reach the in-cluster
    service as well as this host, and a knob per image is a knob per place to forget.
    """
    return os.environ.get("ROBOVAST_PROJECT", "").strip() or DEFAULT_IMAGE_PROJECT


def default_image_tag() -> str:
    """The tag the image family is pulled at.

    :data:`FLOATING_IMAGE_TAG` unless ``ROBOVAST_PROJECT_TAG`` pins one, and resolving to
    it warns (see :func:`resolve_family_image`).

    Not derived from the installed version: CI publishes semver tags only for a ``v*`` push,
    so an unreleased version or an editable install would name an image that does not
    exist. A default has to be a tag CI produces on every merge to the default branch, and
    ``latest`` is the only one that is. A release is pinned by naming its version in
    ``ROBOVAST_PROJECT_TAG``.
    """
    return os.environ.get("ROBOVAST_PROJECT_TAG", "").strip() or FLOATING_IMAGE_TAG


def resolve_family_image(image: str, *, project: str | None = None,
                         tag: str | None = None, role: str = "container image",
                         warn: bool = True) -> str:
    """Resolve a ``family:<member>`` ref to ``<project>/<member>:<tag>``.

    *project* and *tag* come from the campaign when there is one (so a single campaign
    can run against a dev project without touching the deployment) and from the
    environment otherwise. They are passed explicitly rather than read from ambient
    state here because a campaign is composed in a worker thread, and sometimes in an
    isolated subprocess -- neither of which a module-level value survives, and both of
    which run concurrently with campaigns configured differently.
    """
    member = family_member(image)
    resolved = f"{project or default_image_project()}/{member}:{tag or default_image_tag()}"
    if warn and resolved.endswith(f":{FLOATING_IMAGE_TAG}"):
        # A run whose image is a floating tag is not reproducible, and the person who has
        # to know that is the one starting it. Not an error -- a floating tag is the right
        # answer for a dev loop and for an editable install, which has no release tag to
        # match.
        #
        # *warn* exists because that reasoning is about starting a run. The cluster
        # lifecycle commands start none, and floating is the mode they are built for:
        # `upgrade` restarts unconditionally *because* a floating tag leaves the
        # Deployment spec byte-identical, and reports the digest it landed on; the warm
        # DaemonSet forces the re-pull a floating tag would otherwise skip. There the
        # digest is the honest report and the warning is noise -- four copies of it on
        # one `cluster setup`, all derived from the same `default_image_tag()`.
        logger.warning(
            "%s resolved to %r, a floating tag: what this runs against is whatever was "
            "last pushed there. Set ROBOVAST_PROJECT_TAG to pin it.", role, resolved)
    return resolved


def resolve_family_images_in_containers(containers: dict | None, *,
                                        project: str | None = None,
                                        tag: str | None = None) -> dict | None:
    """Resolve every ``family:`` ref in an ``execution.containers`` mapping, in place.

    Called once per campaign, right after a simulator backend has filled its container
    blocks in, so that everything downstream -- the container plan, the image builds, the
    run environment, and ``_execution/execution.yaml`` -- reads concrete refs. Resolving
    later instead would leave a ``family:`` ref in the campaign's own record, and
    postprocessing reads that record to pick the image it deserializes rosbags in.

    Only ``family:`` refs are touched. A ref the ``.vast`` states is left byte-identical,
    digest and all: that field names the campaign's own image, and rewriting it would run
    something the author did not ask for.
    """
    for name, block in (containers or {}).items():
        if not isinstance(block, dict) or not is_family_image_ref(block.get("image")):
            continue
        block["image"] = resolve_family_image(
            block["image"], project=project, tag=tag,
            role=f"image for container '{name}'")
    return containers


def _resolve_image(member: str | None, *, explicit: str | None = None,
                   config_image: str | None = None, project: str | None = None,
                   tag: str | None = None, role: str = "container image",
                   warn: bool = True) -> str:
    """Resolve a container image with a fixed precedence.

    Precedence (highest first): *explicit* (e.g. a ``--image`` flag) → *config_image*
    (a value from the ``.vast``) → the family default for *member*.

    Whatever wins, a symbolic ref is resolved here and only here. There are two, and the
    difference matters: a ``build:`` ref must already have been made concrete by the
    build lifecycle, so one arriving here is a bug and raises; a ``family:`` ref is
    *meant* to arrive symbolic, because this is the layer that knows the project and tag.

    *member* is ``None`` where there is no family default to fall back on — a sidecar
    container, whose image nothing but the campaign can name. Guessing the framework
    image for it would run something nobody asked for, so it fails loudly instead.
    """
    if explicit:
        resolved = explicit
    elif config_image:
        resolved = config_image
    elif member:
        resolved = family_image_ref(member)
    else:
        # CampaignConfigError, not ValueError: this is bad input with a self-contained,
        # actionable message, and `failure_detail` drops the traceback for it. A stack
        # trace here reads as a RoboVAST bug rather than as something the author has to
        # go and configure -- the same reason cluster_service raises it for an
        # unconfigured registry.
        raise CampaignConfigError(
            f"no image configured for this {role}: set "
            "execution.containers.<name>.image in the .vast. There is no family default "
            "for a container RoboVAST does not own — an image nobody named is not a "
            "default.")
    if is_build_image_ref(resolved):
        raise CampaignConfigError(
            f"unresolved build image ref '{resolved}': the 'build:' image must be "
            "built (build_experiment_image / the start_campaign preflight) before "
            "it can be used as a container image")
    if is_family_image_ref(resolved):
        resolved = resolve_family_image(resolved, project=project, tag=tag, role=role,
                                        warn=warn)
    return resolved


def resolve_robovast_image(explicit: str | None = None,
                           config_image: str | None = None, *,
                           fallback: bool = True, project: str | None = None,
                           tag: str | None = None) -> str:
    """Resolve the image a campaign's scenario container runs in.

    Defaults to :data:`MEMBER_ROBOVAST` — the RoboVAST contract image — which is also
    the ``FROM`` experiment images build on (:func:`resolve_build_base_image`).

    *fallback* is ``False`` for a container that is **not** the main one: a sidecar or
    system-under-test has no family default, and inventing one would launch an image the
    campaign never named.
    """
    return _resolve_image(MEMBER_ROBOVAST if fallback else None,
                          explicit=explicit, config_image=config_image,
                          project=project, tag=tag)


def resolve_build_base_image(config_image: str | None = None, *,
                             project: str | None = None,
                             tag: str | None = None) -> str:
    """Resolve the ``FROM`` an experiment image is built on (``build.base_image``).

    Spelled out separately from :func:`resolve_robovast_image` so the *role* in any
    warning names ``build.base_image``: a campaign reaching here has its
    ``execution.image`` set (to the ``build:`` ref that got us here), so a warning about
    ``execution.image`` reads as a bug in the campaign that is not there.
    """
    return _resolve_image(MEMBER_ROBOVAST, config_image=config_image,
                          project=project, tag=tag, role="build base image")


def resolve_controller_image(explicit: str | None = None,
                             config_image: str | None = None) -> str:
    """Resolve the robovast-controller image (the ``vast serve`` Deployment).

    Cluster-side and never per-campaign: this image is chosen when the service is
    deployed, so it takes the project from the environment ``vast cluster
    upgrade`` runs in.

    No floating-tag warning (``warn=False``): the callers are ``setup`` and ``upgrade``,
    which report the digest the pod actually landed on once the rollout has converged.
    That is the fact the warning was approximating, and it is available here where it is
    not at campaign-composition time.
    """
    return _resolve_image(MEMBER_CONTROLLER, explicit=explicit,
                          config_image=config_image, role="controller image", warn=False)


#: BuildKit secret id for the git token, and the one name both sides of a build agree on: the
#: service renders ``--mount=type=secret,id=...`` into the Dockerfile, and the image build
#: passes the matching ``--secret id=...`` to the builder. The same id
#: ``container/robovast/Dockerfile.roqsim`` and ``build.sh`` use for their own clone -- one
#: convention, so an operator configures a token once.
#:
#: It lives in ``common`` because it is a contract *between* the two layers, and the engine
#: importing it from ``service`` was the last remaining ``execution -> service`` dependency at
#: module load -- the inversion ``tests/execution/test_layering.py`` exists to catch. Same
#: reasoning, and the same move, as ``common/build_context.py``'s docstring records for the
#: ignore set.
GIT_TOKEN_SECRET_ID = "git_token"


def resolve_sidecar_image(explicit: str | None = None) -> str:
    """Resolve the robovast-sidecar image: the data-plane transfers of every pod.

    Resolved *inside* the service (a campaign Job's ``fetch-inputs`` and ``uploader``
    containers, an aux pod's transfer container, the postprocessing Job's ``stage``, and
    the image-build Job's context fetch all call this from there), so the project it uses
    is the one carried into the service pod's environment — see
    :func:`~...service_deploy.service_manifests`.
    """
    return _resolve_image(MEMBER_SIDECAR, explicit=explicit, role="sidecar image")


#: Env var carrying the revision this code was built from, set into the image at build
#: time (``container/image_stamp.sh`` -> ``ARG``/``ENV``). It exists because the git
#: lookup below **cannot** work in a deployed image: the package is installed, so there is
#: no ``.git`` above ``site-packages/robovast/common/``, and ``git rev-parse`` there finds
#: no repository however present the git binary is. Baking it at build time is the only
#: point where the revision is knowable.
GIT_REVISION_ENV = "ROBOVAST_GIT_REVISION"


def code_revision() -> str:
    """The revision this process's code was built from, or ``""`` when not determinable.

    Separate from :func:`get_app_version` because the two answer different questions and
    one string cannot do both: a *version* is for the client/service compatibility
    handshake, where a semver comparison is what is wanted, while a *revision* answers "is
    the change I just made loaded?" — which a long-lived service makes a real question and
    which a semver cannot answer, because it stays ``2.0.0`` across every edit.

    ``""`` is a deliberate, meaningful answer: **this deployment cannot tell you**. Reporting
    a version here instead would look like a revision that happens not to match, so a caller
    checking for staleness would read "different code" where the truth is "no information" —
    the same confusion that had a missing docker CLI reported as an unbuilt image.
    """
    baked = os.environ.get(GIT_REVISION_ENV, "").strip()
    if baked:
        return baked
    revision = _git_revision()
    return revision or ""


def _git_revision() -> "str | None":
    """``<short-sha>[+dirty]`` from the checkout this module lives in, or ``None``.

    Only ever answers for a source checkout. See :data:`GIT_REVISION_ENV` for why an
    installed copy in an image cannot be asked this way.
    """
    module_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        sha = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            stderr=subprocess.STDOUT, cwd=module_dir, text=True).strip()
        dirty = subprocess.check_output(
            ['git', 'status', '--porcelain'],
            stderr=subprocess.STDOUT, cwd=module_dir, text=True).strip()
    except Exception:  # noqa: BLE001 - no repo, no git binary: not a revision, not an error
        return None
    return f"{sha}+dirty" if dirty else sha


#: Env var carrying the wall-clock time the image was built, set alongside
#: :data:`GIT_REVISION_ENV` by ``container/image_stamp.sh``. Baked for the same reason and a
#: sharper one: a container cannot read its own labels, so ``org.opencontainers.image.created``
#: -- which every published image carries -- is invisible to the process running inside it.
BUILD_DATE_ENV = "ROBOVAST_BUILD_DATE"


def build_date() -> str:
    """When this process's image was built (RFC 3339, UTC), or ``""`` when not determinable.

    Answers the question a revision cannot: *how old is what is deployed?* A short sha is only
    comparable against a checkout, so an operator looking at a service — deciding whether the
    build in front of them predates a fix — has nothing to read it against. A timestamp is
    legible on its own.

    ``""`` carries the same meaning it does in :func:`code_revision`: **this deployment cannot
    tell you**. A source checkout has no build to date, and neither has an image built by hand
    without the build arg. Substituting anything here — the file's mtime, today's date — would
    manufacture an answer to a question that has none, and it would be believed.
    """
    return os.environ.get(BUILD_DATE_ENV, "").strip()


#: How many changed paths a provenance record keeps. A dirty tree can hold thousands, and a
#: record that balloons stops being read; the count beside the sample keeps the fact intact.
MAX_RECORDED_CHANGED_PATHS = 20


def check_image_compat(image: str, *, version: "int | None" = None,
                       source: str = "", unreadable: bool = False) -> "str | None":
    """``None`` when this host can drive *image*, else a message saying what to do.

    *version* is what the image reports, ``None`` when it reports nothing; *source* says
    what answered. *unreadable* marks a ``None`` that means the image could not be read at
    all, which is a fault in reaching it rather than a fact about the image.
    """
    if version is None and unreadable:
        return (f"cannot determine the container protocol version of {image!r}: {source}.\n"
                f"The image itself was never read, so this says NOTHING about the image and "
                f"rebuilding it will not change this. Check what stopped the read: the "
                f"deployment's registry credential (the pull Secret the campaign's namespace "
                f"holds), the registry's reachability from the controller, and its CA if TLS "
                f"is in play.\n"
                f"If you already know the image is one this host can drive, "
                f"ROBOVAST_SKIP_IMAGE_COMPAT_CHECK=1 is the documented way past this -- it is "
                f"exactly the case that switch exists for.")
    if version is None:
        return (f"cannot determine the container protocol version of {image!r}: {source}.\n"
                f"This host speaks {MIN_IMAGE_COMPAT}..{COMPAT_VERSION}. An image that reports "
                f"nothing is either not a robovast image or predates the {COMPAT_VERSION_LABEL} "
                f"label.\n"
                f"If it is a robovast image and you have its source, rebuild it from the "
                f"revision the campaign recorded (_execution/execution.yaml: robovast_revision) "
                f"-- the rebuilt image carries the label. If you know which protocol it speaks, "
                f"re-tag it with that label rather than guessing here.")
    if MIN_IMAGE_COMPAT <= version <= COMPAT_VERSION:
        return None
    if version > COMPAT_VERSION:
        return (f"{image!r} speaks container protocol {version} (from its {source}), but this "
                f"host speaks {MIN_IMAGE_COMPAT}..{COMPAT_VERSION}. The image is NEWER than "
                f"this robovast -- upgrade robovast rather than rebuilding the image.")
    return (f"{image!r} speaks container protocol {version} (from its {source}), but this host "
            f"speaks {MIN_IMAGE_COMPAT}..{COMPAT_VERSION} and no longer supports {version}.\n"
            f"Either check out the robovast revision the campaign's results recorded "
            f"(_execution/execution.yaml: robovast_revision) and run it there, or rebuild the "
            f"image from that revision. Do NOT pull a newer image: a re-run needs the bytes "
            f"the campaign recorded, not today's.")


def code_provenance() -> dict:
    """What identifies the code that composed this campaign, for the campaign's own record.

    Distinct from both :func:`code_revision` and :func:`get_app_version`, and for a reason
    neither can serve: those answer "is my change loaded?" and "which version am I talking
    to?", where a short sha or a semver is fine. This answers **"which commit do I check out
    to re-run this a year from now?"** -- so it needs a *full* sha, and it must say when it
    does not have one rather than returning something that merely looks like an identifier.

    Returns a dict with only the keys it can actually answer:

    ``revision``
        Full 40-character sha when this is a source checkout; the baked value when running
        from an image, which is short because that is what was baked. Absent when neither.
    ``revision_source``
        ``"git"`` or ``"baked"`` -- so a reader knows whether ``revision`` is a full sha.
        Without it, a short baked value is indistinguishable from a truncated full one.
    ``dirty``
        Whether the checkout had uncommitted changes. **A dirty campaign is not
        reproducible**, because the recorded sha does not describe the code that ran, and
        that has to be recorded rather than inferred later from a missing field.
    ``changed_paths`` / ``changed_count``
        A capped sample and the true total, present only when dirty.

    An empty dict is a meaningful answer: this deployment cannot tell you.
    """
    baked = os.environ.get(GIT_REVISION_ENV, "").strip()
    if baked:
        # Trusted verbatim, exactly as `code_revision` does: in a pod there is no `.git` to
        # ask, so this is the only thing that can answer. The `+dirty` suffix is the build's
        # own report and is unpacked rather than left inside the identifier.
        revision, _, suffix = baked.partition("+")
        return {"revision": revision, "revision_source": "baked",
                "dirty": suffix == "dirty"}

    module_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        sha = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            stderr=subprocess.STDOUT, cwd=module_dir, text=True).strip()
        status = subprocess.check_output(
            ['git', 'status', '--porcelain'],
            stderr=subprocess.STDOUT, cwd=module_dir, text=True)
    except Exception:  # noqa: BLE001 - no repo, no git binary: not an error, just no answer
        return {}

    record = {"revision": sha, "revision_source": "git", "dirty": bool(status.strip())}
    if record["dirty"]:
        paths = [line[3:].strip() for line in status.splitlines() if len(line) > 3]
        record["changed_count"] = len(paths)
        record["changed_paths"] = sorted(paths)[:MAX_RECORDED_CHANGED_PATHS]
    return record


#: Labels a robovast image carries about its own build. The OCI names where they exist -- no
#: reason to invent our own -- and ``org.robovast.*`` only for what OCI does not model.
_BUILD_REF_LABELS = {
    "revision": "org.opencontainers.image.revision",
    "source": "org.opencontainers.image.source",
    "roqsim_ref": "org.robovast.roqsim-ref",
    "scenario_execution_ref": "org.robovast.scenario-execution-ref",
    # Written by the Dockerfile and, until now, collected by nothing -- so a campaign recorded
    # three of the four source refs its image was built from.
    "scenario_execution_server_ref": "org.robovast.scenario-execution-server-ref",
    # The rest of the recipe: what a rebuild starts FROM, and the dated archives that make it
    # install the same versions rather than whatever is current. Without these, "rebuild it"
    # means "rebuild something like it".
    "base_image": "org.robovast.base-image",
    "ubuntu_snapshot": "org.robovast.ubuntu-snapshot",
    "ros_snapshot": "org.robovast.ros-snapshot",
}


def image_build_refs(containers: dict, labels_by_role: "dict | None" = None) -> dict:
    """``{role: {...}}`` naming what each container's image was built from.

    Answers the question a re-run asks once the image itself is gone: *rebuild it from what?*
    A digest identifies bytes but says nothing about their origin, so a campaign holding only
    digests is reproducible exactly as long as the registry keeps them and not one day longer.

    Two independent sources, merged, because they cover different tiers:

    * **labels on the image** -- for anything robovast built or publishes. The refs it was built
      from are build ARGs, invisible from outside the build, so without the labels the only way
      to answer is to read the Dockerfile at the recorded robovast commit *and* hope the ref was
      not overridden at build time.
    * **the container's declared ``provenance:``** -- for a user-supplied image, where robovast
      cannot know and the author is the only source.

    Absent entries mean "not knowable here", never "nothing to record": a rebuild would follow
    a guess. *labels_by_role* are the labels the caller read from the registry.
    """
    out: dict = {}
    for role, block in sorted((containers or {}).items()):
        block = block or {}
        entry = {}
        supplied = (labels_by_role or {}).get(role) or {}
        for key, label in _BUILD_REF_LABELS.items():
            value = supplied.get(label, "")
            if value:
                entry[key] = value

        declared = block.get("provenance")
        if isinstance(declared, dict):
            # The author's answer wins over anything read from the image: they are describing an
            # image robovast did not build, so a label found there was put by somebody else and
            # may describe a base rather than this image.
            entry.update({key: value for key, value in declared.items() if value})
            entry["declared"] = True
        if entry:
            out[role] = entry
    return out


def campaign_code_provenance() -> dict:
    """:func:`code_provenance` for a campaign about to run, warning when it is not reproducible.

    The warning belongs here rather than at each call site so it is reported
    identically and exactly once per campaign. It is a warning and not a refusal on purpose:
    running from a dirty tree is the normal research loop, and blocking it would only teach
    people to bypass the check. What must not happen is the campaign *looking* reproducible
    afterwards -- so the fact is recorded either way.
    """
    record = code_provenance()
    if not record:
        logger.warning(
            "cannot determine which robovast revision is composing this campaign (no git "
            "checkout and no baked %s). Its results will not say what code produced them, "
            "so re-running it later cannot be verified.", GIT_REVISION_ENV)
    elif record.get("dirty"):
        count = record.get("changed_count", 0)
        logger.warning(
            "composing this campaign from a DIRTY robovast checkout (%s at %s, %d changed "
            "path(s)). The recorded revision does not describe the code that ran, so this "
            "campaign cannot be reproduced from it -- commit first if that matters.",
            record.get("revision_source", "?"), record.get("revision", "?")[:12], count)
    return record


def _build_refs_yaml(refs: dict) -> str:
    """Render :func:`image_build_refs` as an ``image_build_refs:`` block, or ``""``.

    Dumped with yaml rather than hand-formatted: this is nested, and a mis-indented nested
    mapping produces a file that parses as something else entirely and nothing notices.
    """
    if not refs:
        return ""
    return yaml.dump({"image_build_refs": refs}, default_flow_style=False, sort_keys=True)




def get_app_version() -> str:
    """Return a short version string for the robovast package.

    Resolution order:
    0. The revision baked into the image at build time (:data:`GIT_REVISION_ENV`) — the only
       one of these that can answer in a deployed image.
    1. Git short SHA (works for local editable installs).
       If the working tree has uncommitted changes, ``+dirty`` is appended.
    2. Installed package metadata (works for PyPI installs).
    3. ``"unknown"`` as a last-resort fallback.

    A caller that needs to distinguish "a revision" from "only a package version" should ask
    :func:`code_revision`, which returns ``""`` rather than substituting the latter.
    """
    # 0/1. A revision, however it can be had — baked in at build time, else this checkout.
    revision = code_revision()
    if revision:
        return revision

    # 2. Fall back to installed package metadata
    try:
        return pkg_version('robovast')
    except PackageNotFoundError:
        pass

    # 3. Final fallback
    return 'unknown'


logger = logging.getLogger(__name__)


def _check_static_cpu_manager(k8s_client, node_name):
    """Query kubelet configz endpoint to determine the CPU manager policy for a node.

    Args:
        k8s_client: CoreV1Api instance
        node_name: Name of the node to query

    Returns:
        str or None: The cpuManagerPolicy value (e.g. "static", "none") when it
        could be read, or ``None`` when the query failed. ``None`` means *unknown*,
        never "policy is none" — the two are recorded and warned about differently,
        so a failed configz read is never silently logged as pinning-disabled.
    """
    try:
        response = k8s_client.connect_get_node_proxy_with_path(node_name, "configz")
        data = json.loads(response)
        kubelet_config = data.get("kubeletconfig", {})
        return kubelet_config.get("cpuManagerPolicy")
    except Exception as exc:
        logger.debug("Could not retrieve kubelet configz for node %s: %s", node_name, exc)
        return None


def _get_cluster_info(context=None):
    """Collect basic cluster information for cluster executions.

    Args:
        context: Kubernetes context name to use. ``None`` uses the active context.

    Returns a dictionary with node_count, node_labels, cpu_manager_policy and
    cluster_config (read from the deployed robovast-service) when available.
    Failures are logged and result in partial or empty data rather than errors.
    """
    cluster_info = {}

    # Best-effort cluster-config metadata, read from the deployed robovast-service
    # (the authoritative record; there is no local flag file). Non-fatal.
    try:
        from robovast.execution.cluster_execution.service_deploy import \
            read_service_config_from_cluster  # pylint: disable=import-outside-toplevel
        name, kwargs = read_service_config_from_cluster(kube_context=context)
        if name is not None:
            cluster_info["cluster_config"] = {"name": name, "kwargs": kwargs}
    except Exception as exc:  # pragma: no cover - best-effort, non-fatal
        # Expected wherever the service is not reachable (no kubeconfig, in-pod
        # controller, etc.); keep at debug to avoid noise.
        logger.debug("Cluster config metadata unavailable: %s", exc)

    # Collect node information via Kubernetes Python API
    node_count = None
    node_labels = {}
    cpu_manager_policies = {}
    try:
        from kubernetes import client as k8s_client_lib  # pylint: disable=import-outside-toplevel

        from robovast.execution.cluster_execution.kube_client import \
            load_kube_config  # pylint: disable=import-outside-toplevel
        load_kube_config(context=context)

        v1 = k8s_client_lib.CoreV1Api()
        node_list = v1.list_node()
        items = node_list.items or []
        node_count = len(items)

        for node in items:
            name = node.metadata.name
            # Keyed by the hashed label, and with the hostname label dropped, because this
            # block is lifted verbatim into campaign.execution_json and travels with every
            # published campaign. ``kubernetes.io/hostname`` merely restates the key, so
            # keeping it would re-add by value exactly what hashing the key removed.
            labels = {k: v for k, v in (node.metadata.labels or {}).items()
                      if k != "kubernetes.io/hostname"}
            if name:
                node_labels[node_label(name)] = labels
                policy = _check_static_cpu_manager(v1, name)
                if policy is None:
                    # Query failed: unknown, not "none". Surface it — a node whose
                    # policy we cannot read may lack the pinning that deterministic
                    # scenario timing needs, and we must not record it as "none".
                    logger.warning(
                        "Could not determine CPU manager policy for node %s; "
                        "deterministic scenario timing is not guaranteed.", name)
                else:
                    cpu_manager_policies[node_label(name)] = policy

        # Warn about nodes without static CPU pinning (recorded provenance keeps
        # only the policies we could actually read).
        non_static = [n for n, p in cpu_manager_policies.items() if p != "static"]
        if non_static:
            logger.warning(
                "Static CPU Manager policy is NOT enabled on %d node(s): %s; "
                "scenario timing may be non-deterministic.",
                len(non_static), ", ".join(non_static))

    except Exception as exc:  # pragma: no cover - best-effort, non-fatal
        logger.warning("Failed to collect cluster node information: %s", exc)

    if node_count is not None:
        cluster_info["node_count"] = node_count
    if node_labels:
        cluster_info["node_labels"] = node_labels
    if cpu_manager_policies:
        cluster_info["cpu_manager_policies"] = cpu_manager_policies

    return cluster_info or None


# Regex that matches any campaign directory name: <name>-YYYY-MM-DD-HHMMSS
# The default name prefix is "campaign" for backward compatibility.
_CAMPAIGN_DIR_RE = re.compile(r'^.+-\d{4}-\d{2}-\d{2}-\d{6,8}$')


def is_campaign_dir(name: str) -> bool:
    """Return True if *name* looks like a campaign directory.

    Both the legacy ``campaign-YYYY-MM-DD-HHMMSS`` format and the newer
    ``<metadata-name>-YYYY-MM-DD-HHMMSScc`` format (with hundredths of a
    second for concurrent-run disambiguation) are recognized.
    """
    return bool(_CAMPAIGN_DIR_RE.match(name))


def get_campaign_timestamp(dir_name: str) -> str:
    """Extract the timestamp portion from a campaign directory name.

    Works for both ``campaign-YYYY-MM-DD-HHMMSS`` and
    ``<name>-YYYY-MM-DD-HHMMSScc``.  Returns the full *dir_name* unchanged
    when the expected suffix cannot be found.
    """
    m = re.search(r'(\d{4}-\d{2}-\d{2}-\d{6,8})$', dir_name)
    return m.group(1) if m else dir_name


def get_campaign(name: str = "campaign") -> str:
    """Return a unique campaign directory name.

    Args:
        name: Campaign name prefix taken from ``metadata.name`` in the ``.vast``
              file.  Defaults to ``"campaign"`` for backward compatibility.

    Returns:
        A string of the form ``<name>-YYYY-MM-DD-HHMMSScc`` where *cc* are
        hundredths of a second.  The extra precision virtually eliminates
        campaign-ID collisions when multiple ``vast workspace run``
        invocations start in the same second.
    """
    now = datetime.datetime.now()
    return f"{name}-{now.strftime('%Y-%m-%d-%H%M%S')}"


def get_execution_env_variables(run_num, config_name, additional_env=None):
    """Get environment variables for execution.

    Args:
        run_num: Run number
        config_name: Configuration name
        additional_env: Optional list of additional environment variables in format:
                       [{"KEY": "value"}]

    Returns:
        Dictionary of environment variables
    """
    campaign_id = get_campaign()
    env_vars = {
        'CAMPAIGN_ID': campaign_id,
    }

    # Add custom environment variables from execution config
    if additional_env and isinstance(additional_env, list):
        for env_item in additional_env:
            if isinstance(env_item, dict):
                # Handle simple format: {"KEY": "value"}
                for key, value in env_item.items():
                    env_vars[key] = value

    return env_vars


def scenario_env(campaign_data):
    """The scenario-shaping env vars a run's config implies, for ``entrypoint.sh``.

    Covers only what is derived from the ``.vast``: which scenario file to run, the
    simulation backend, the runner selection, and whether the behaviour tree status log is
    recorded. Shared by the Kubernetes backend and container-exec, so the two cannot drift.

    Deliberately *not* here:

    - **Path-valued vars** (``SCENARIO_PARAMETER_FILE``, ``OUTPUT_DIR``,
      ``SCENARIO_OUTPUT_DIR``). Those depend on the mount layout and job packing — the
      caller owns them.
    - **``SCENARIO_EXECUTION_PARAMETERS``**. The Kubernetes backend derives it from
      ``log_tree``.
    """
    execution = campaign_data.get("execution") or {}
    env = {
        'SCENARIO_FILE': os.path.basename(
            campaign_data.get("scenario_file", "scenario.osc")),
    }
    simulation = execution.get("simulation")
    if simulation:
        env['SIMULATION'] = str(simulation)
    # 'auto' is the entrypoint's own default (detect ros2 on PATH); passing it would
    # only restate that, so it stays unset.
    mode = execution.get("mode", "auto")
    if mode and mode != "auto":
        env['SCENARIO_MODE'] = str(mode)
    # Always on, and stated rather than left to the entrypoint's own default, so the compose
    # file / pod spec says outright what the run did. A run that did not record how its
    # behaviour tree progressed cannot be explained after the fact, and the file costs
    # ~100 KB beside a multi-MB rosbag -- there is no campaign worth turning it off for,
    # so there is no way to.
    #
    # Not routed through SCENARIO_EXECUTION_PARAMETERS: the Kubernetes backend overwrites
    # that whole variable with '-t', which would drop the flag on exactly the runs whose tree
    # state is hardest to inspect.
    #
    # An execution image whose scenario_execution predates --bt-log ignores the flag rather
    # than failing (both runners use parse_known_args), so the run still succeeds; it just
    # produces no behaviors.jsonl.
    env['BT_LOG'] = 'true'

    # What the entrypoint's own (wall-time) recorder captures, in WALL time and for the
    # whole container's life -- distinct from the scenario's ``bag_record``, which is
    # sim-time and starts mid-run. Exactly what the ``run_log`` table needs and no more:
    # ``/rosout`` for the lines, ``/clock`` to put a wall-stamped line on the playback
    # clock. Stated for the same reason BT_LOG is.
    #
    # Not configurable from ``execution:``. What a run records *beyond* this is the
    # scenario's ``bag_record`` to say, where it sits beside the behaviour that produces it.
    env['LOG_TOPICS'] = '/rosout /clock'

    # A simulator backend's environment, resolved *here* rather than by each emitter, so
    # every emitter applies one rule: the campaign's own execution.env wins, because a
    # backend supplies defaults it knows, not decisions it takes away.
    env.update(_backend_env_for(execution))
    return env


def _backend_env_for(execution: dict) -> dict:
    """The backend's contribution, with the campaign's own ``execution.env`` winning."""
    backend_env = execution.get("_backend_env") or {}
    if not backend_env:
        return {}
    authored = set()
    for entry in (execution.get("env") or []):
        authored.update(entry.keys() if isinstance(entry, dict) else [entry])
    return {k: str(v) for k, v in backend_env.items() if k not in authored}


def sidecar_backend_env(execution: dict, container_name: str) -> dict:
    """The backend's environment for a SIDECAR, which :func:`scenario_env` cannot reach.

    ``scenario_env`` emits the backend's contribution into the *main* container, which is
    right in the stepped shape: there the simulator runs in the scenario's own process, so
    the main container IS the simulator. In the ROS shape the simulator is a sidecar, and
    the same variables have to arrive there instead -- otherwise the simulator never sees
    them. That is not hypothetical: roqsim's ``ROQSIM_RECORD`` /
    ``ROQSIM_CAPTURE_EXPORT_DIR`` went only to the scenario container, so a ROS campaign
    produced no ``run.npz`` and no ``capture/`` while ``produces_run_capture()`` still
    reported True and validation happily accepted a ``scene3d`` panel with nothing to
    replay. The stepped shape hid it, because there the two containers are one.

    Only the ``simulation`` container: a backend describes its own simulator, and handing
    ``ROQSIM_*`` to a vanilla nav2 SUT would be noise that reads like configuration.

    A relative path in those variables resolves against ``RUN_OUTPUT_DIR``, which every
    sidecar is already given -- so a per-run artifact lands in the run's own
    directory rather than at the campaign root where each run would overwrite the last.
    """
    if container_name != SIMULATION_CONTAINER:
        return {}
    return _backend_env_for(execution)


_LOCAL_INIT_BLOCK = "command -v fixuid > /dev/null 2>&1 || { echo 'ERROR: fixuid not found in container image. Please rebuild the image.' >&2; exit 1; }; eval $(fixuid -q)\nEXTRA_REQUIRED_TOOLS=\"fixuid\""

# A cluster run needs no tool beyond the base set: its inputs are landed by the pod's init
# container and its outputs are delivered by the pod's uploader container, both from the
# sidecar image, so the experiment image carries nothing that reaches storage.
_CLUSTER_INIT_BLOCK = "EXTRA_REQUIRED_TOOLS=\"\""

# Used when the caller has no cluster provider to ask: a run that is not an instance of
# anything records an empty instance_type (which ingests as NULL).
# ``|| true`` is not needed here, but a provider's command must never abort the run —
# sysinfo collection is explicitly non-fatal — so each implementation keeps its own
# failure tolerance (a metadata-server curl that 404s yields an empty string).
_NO_INSTANCE_TYPE = 'INSTANCE_TYPE=""'

#: The environment a run's own tools live in, as shell.
#:
#: Everything colcon-built in these images -- ``scenario_execution`` first among them -- is
#: importable ONLY after these two setups are sourced, and the images deliberately do not put
#: them in any shell rc (a login shell reads ``/etc/profile``, not ``/ws/install/setup.bash``).
#: So a diagnostic that ``docker exec``s a bare argv into a live run cannot see the run's own
#: modules, in any image, however freshly built -- which is what :func:`in_run_env` exists to
#: stop, and what a rebuild is repeatedly mistaken for a fix of.
#:
#: ROS-optional by the same guards the entrypoint uses, so an image with no ROS in it runs the
#: command unchanged rather than failing on a missing file.
#: ``--`` on both, and on the secondary entrypoint's own copy: ROS's ``setup.bash`` reads the
#: *caller's* positional parameters when it is given none of its own, so sourcing it from a script
#: that has any leaves a sidecar's arguments interpreted by the ROS setup.
#:
#: ``ROS_SETUP_ANNOUNCE`` is the one thing the copies legitimately differed in: an entrypoint logs
#: the step into the run's log, and a live exec has no ``log`` function to call. Defaulted to the
#: shell's no-op so the block runs anywhere, rather than each caller keeping its own copy for the
#: sake of one line.
#: **Every branch announces itself, including the ones that do nothing.** A setup that skips
#: silently makes "No module named 'scenario_execution'" unreadable: a missing overlay and a
#: genuinely absent module produce the same message, so a reader cannot tell an image problem
#: from a plumbing one, and each guess costs a campaign to test. Saying which branch ran makes
#: that message an answer rather than another round.
ROS_SETUP_BLOCK = """\
if [ -z "${ROS_DISTRO:-}" ]; then
    ${ROS_SETUP_ANNOUNCE:-:} "no ROS_DISTRO set, so no ROS overlay was sourced"
elif [ ! -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]; then
    ${ROS_SETUP_ANNOUNCE:-:} "ROS_DISTRO=${ROS_DISTRO} but /opt/ros/${ROS_DISTRO}/setup.bash is missing"
else
    . "/opt/ros/${ROS_DISTRO}/setup.bash" --
    if [ -f /ws/install/setup.bash ]; then
        . /ws/install/setup.bash --
        ${ROS_SETUP_ANNOUNCE:-:} "sourced /opt/ros/${ROS_DISTRO} and /ws/install"
    else
        ${ROS_SETUP_ANNOUNCE:-:} "sourced /opt/ros/${ROS_DISTRO}; this container has no /ws/install overlay"
    fi
fi"""

#: Prefix on every note the block emits from a live exec, so a caller can tell our sentence from
#: the command's own output when it reads back a failure.
RUN_ENV_NOTE = "robovast-env:"


def in_run_env(command: str) -> list:
    """*command* as argv that runs it in the environment a run's own processes have.

    For reaching into a **live** job, where the run's ``entrypoint.sh`` is not an option: it
    collects sysinfo, starts Xvfb and the resource monitor and tees into the run's log, so
    invoking it would perturb the very run being diagnosed. This is the environment part of it
    and nothing else.

    *command* is a string because that is how the things asked for here are written -- a
    backend's ``health_command`` returns one, and a probe is whatever a caller would have typed.

    ``bash -c`` and deliberately not ``-lc``: the overlay is sourced here, explicitly, so a login
    shell adds ``/etc/profile``'s opinion about ``PATH`` and buys nothing -- and what a login shell
    does or does not read was itself one of the guesses that made this hard to diagnose.

    **The command is a shell body, not an ``exec`` argument.** An ``exec {command}`` silently
    runs only the first simple command: everything after a ``;`` is dropped without a word, and a
    braced group is a syntax error. A probe that returns the output of the first third of what you
    typed is worse than one that refuses, because it looks like an answer. The exit status is the
    last command's either way, which is all ``exec`` would buy.

    The notes go to **stderr**, where they join whatever the command says about its own failure
    rather than being mixed into the JSON a caller parses.
    """
    prelude = (f'{_RUN_ENV_NOTE_FN}() {{ echo "{RUN_ENV_NOTE} $*" >&2; }}\n'
               f'ROS_SETUP_ANNOUNCE={_RUN_ENV_NOTE_FN}\n')
    return ["/bin/bash", "-c", f"{prelude}{ROS_SETUP_BLOCK}\n{command}"]


#: Shell function name the notes go through. Named rather than inlined so the block's
#: ``${ROS_SETUP_ANNOUNCE}`` seam carries a *command*, which is what the entrypoints put there too.
_RUN_ENV_NOTE_FN = "_robovast_env_note"


# The log helper both entrypoints share, substituted into ``# @@LOG_BLOCK@@``.
#
# One definition because the two scripts must emit the *same* line format: the merged run log
# parses one grammar (``robovast.common.log_summary._STAMP``), and a sidecar whose format drifted
# from the main container's would not error -- it would silently lose its timestamps and fall back
# to inheriting a neighbour's. Duplicated shell is how that drift happens.
#
# The format is deliberately the one every ROS node in these containers already writes, so the
# entrypoint's own lines need no second parser to be placed in time and attributed.
_LOG_BLOCK = """\
# The wall clock for a log stamp, formatted as ROS writes it: `1786264427.117714`.
#
# `EPOCHREALTIME` (bash 5) is preferred because it costs no subprocess, and this runs once per
# logged line. It is *locale-formatted*, though: under a comma-decimal locale it yields
# `1786264427,117714`, which the log grammar does not match and whose loss nothing would report
# -- the lines would simply have no time. Substituting the separator here is safer than forcing
# LC_NUMERIC on the whole container, whose workload we do not want to relocalise.
_now() {
    local t="${EPOCHREALTIME:-}"
    if [ -n "${t}" ]; then
        echo "${t/,/.}"
    else
        date -u +%s.%6N
    fi
}

# Log one line in the `[LEVEL] [epoch] [node]:` form described above.
#
# Writes to stdout ONLY. The redirect below tees stdout into ${LOG_FILE}, so teeing here as
# well would put every line of a non-ROS run's log in that file twice, and make every count
# derived from it wrong by up to 2x.
#
# The level comes from the message when the message declares one. Several call sites say
# "ERROR: ..." in their text, and a stamped level *wins* over the keyword scan that would
# otherwise classify them, so hard-coding INFO here would quietly downgrade every one of those to
# routine output in the log panel's counts and in search_run_logs.
log() {
    local msg="$*" level=INFO
    case "${msg}" in
        ERROR:*|FATAL:*)  level=ERROR ;;
        WARNING:*|WARN:*) level=WARN ;;
    esac
    echo "[${level}] [$(_now)] [entrypoint]: ${msg}"
}"""

#: The emptyDir every container of a cluster pod shares, mounted at this path in each of
#: them: the sockets the scenario drives its sidecars over, and the done markers below.
IPC_DIR = "/ipc"

#: The name the scenario container signs its marker with. A sidecar signs with its own
#: ``CONTAINER_NAME``.
MAIN_CONTAINER = "main"


def done_marker(container: str) -> str:
    """The file a container touches once it has finished writing into ``/out``.

    The one convention the three parties agree on: the scenario container writes
    ``done.main`` as its last post-run step, each sidecar stops its workload on seeing that
    and writes ``done.<name>`` once its own monitor has stopped, and the uploader delivers
    ``/out`` once every marker it was told to wait for exists.
    """
    return f"{IPC_DIR}/done.{container}"


_LOCAL_POST_RUN_BLOCK = """\
    # Build built-in cleanup script (stop rosbag and resource monitor gracefully)
    BUILTIN_CLEANUP_SCRIPT="/tmp/robovast_cleanup.sh"
    cat > "${BUILTIN_CLEANUP_SCRIPT}" << 'CLEANUP_EOF'
#!/bin/bash
if [ -f /tmp/rosbag.pid ]; then
    if start-stop-daemon --stop --signal INT --pidfile /tmp/rosbag.pid >/dev/null 2>&1; then
        _t=0
        while kill -0 $(cat /tmp/rosbag.pid) 2>/dev/null && [ $_t -lt 50 ]; do
            sleep 0.1; _t=$((_t + 1))
        done
        kill -KILL $(cat /tmp/rosbag.pid) 2>/dev/null || true
    fi
    echo "ROS bag process stopped."
fi
if [ -f /tmp/monitor.pid ]; then
    if kill -TERM $(cat /tmp/monitor.pid) 2>/dev/null; then
        _t=0
        while kill -0 $(cat /tmp/monitor.pid) 2>/dev/null && [ $_t -lt 30 ]; do
            sleep 0.1; _t=$((_t + 1))
        done
        kill -KILL $(cat /tmp/monitor.pid) 2>/dev/null || true
    fi
    echo "Resource monitor process stopped."
fi
exit 0
CLEANUP_EOF
    chmod +x "${BUILTIN_CLEANUP_SCRIPT}"

    POST_COMMAND_PARAM="--post-run ${BUILTIN_CLEANUP_SCRIPT}"
    if [ -n "${POST_COMMAND}" ]; then
        if [ -e "${POST_COMMAND}" ]; then
            POST_COMMAND_PARAM="--post-run ${POST_COMMAND} --post-run ${BUILTIN_CLEANUP_SCRIPT}"
            log "Post-command '${POST_COMMAND}' will run before built-in cleanup."
        else
            log "ERROR: Post-command '${POST_COMMAND}' does not exist."
            exit 1
        fi
    fi

    # The runner replaces this shell. /out is a bind mount onto the host here, so nothing has
    # to happen after the runner is gone: what it wrote is already where it is read.
    run_scenario() {
        exec "$@"
    }"""

_CLUSTER_POST_RUN_BLOCK = """\
    # Build built-in cleanup script (stop rosbag and resource monitor gracefully)
    BUILTIN_CLEANUP_SCRIPT="/tmp/robovast_cleanup.sh"
    cat > "${BUILTIN_CLEANUP_SCRIPT}" << 'CLEANUP_EOF'
#!/bin/bash
echo "[cleanup] Starting robovast cleanup (PID=$$)..."
echo "[cleanup] Process tree at cleanup start:"
ps -eo pid,ppid,stat,args 2>/dev/null || ps ax 2>/dev/null || true
echo ""

_stop_daemon() {
    local _name="$1" _pidfile="$2" _signal="$3" _retry="$4"
    if [ ! -f "$_pidfile" ]; then
        echo "[cleanup] ${_name}: no pidfile at ${_pidfile}, skipping."
        return 0
    fi
    local _pid
    _pid=$(cat "$_pidfile" 2>/dev/null)
    if [ -z "$_pid" ]; then
        echo "[cleanup] ${_name}: pidfile ${_pidfile} is empty, removing."
        rm -f "$_pidfile"
        return 0
    fi
    local _state _ppid _comm
    _state=$(awk '/^State:/{print $2}' /proc/$_pid/status 2>/dev/null)
    _ppid=$(awk '/^PPid:/{print $2}' /proc/$_pid/status 2>/dev/null)
    _comm=$(cat /proc/$_pid/comm 2>/dev/null)
    if [ -z "$_state" ]; then
        echo "[cleanup] ${_name}: PID=$_pid not found in /proc (already exited), removing pidfile."
        rm -f "$_pidfile"
        return 0
    fi
    echo "[cleanup] ${_name}: PID=$_pid state=$_state ppid=$_ppid comm=$_comm"
    if [ "$_state" = "Z" ]; then
        echo "[cleanup] ${_name}: PID=$_pid is a zombie (ppid=$_ppid), cannot signal. Removing pidfile."
        rm -f "$_pidfile"
        return 0
    fi
    echo "[cleanup] ${_name}: sending ${_signal} to PID=$_pid (retry=${_retry})..."
    if start-stop-daemon --stop --signal "$_signal" --pidfile "$_pidfile" --retry "$_retry" --remove-pidfile --verbose 2>&1; then
        echo "[cleanup] ${_name}: stopped successfully."
    else
        local _rc=$?
        echo "[cleanup] ${_name}: start-stop-daemon exited with code $_rc."
        local _post_state
        _post_state=$(awk '/^State:/{print $2}' /proc/$_pid/status 2>/dev/null)
        echo "[cleanup] ${_name}: PID=$_pid post-stop state=${_post_state:-gone}"
        rm -f "$_pidfile"
    fi
}

_stop_daemon "rosbag" "/tmp/rosbag.pid" "INT" "INT/30/KILL/5"
_stop_daemon "monitor" "/tmp/monitor.pid" "TERM" "TERM/10/KILL/5"
echo "[cleanup] Cleanup finished."
CLEANUP_EOF
    chmod +x "${BUILTIN_CLEANUP_SCRIPT}"

    # The marker every other container of the pod waits for. The sidecars stop their
    # workloads on seeing it, and the uploader delivers /out once every container's marker
    # exists (see robovast.execution.cluster_execution.pod_upload). Written as the LAST
    # post-run step, so the cleanup above has stopped this container's monitor and rosbag
    # before anything reads their files.
    IPC_DIR="${IPC_DIR:-/ipc}"
    MARK_DONE_SCRIPT="/tmp/robovast_done.sh"
    cat > "${MARK_DONE_SCRIPT}" << 'DONE_EOF'
#!/bin/bash
_marker="${IPC_DIR:-/ipc}/done.main"
touch "${_marker}"
echo "[done] Wrote ${_marker}: the scenario container has finished writing."
DONE_EOF
    chmod +x "${MARK_DONE_SCRIPT}"

    POST_COMMAND_PARAM="--post-run ${BUILTIN_CLEANUP_SCRIPT} --post-run ${MARK_DONE_SCRIPT}"
    if [ -n "${POST_COMMAND}" ]; then
        if [ -e "${POST_COMMAND}" ]; then
            POST_COMMAND_PARAM="--post-run ${POST_COMMAND} --post-run ${BUILTIN_CLEANUP_SCRIPT} --post-run ${MARK_DONE_SCRIPT}"
            log "Post-command '${POST_COMMAND}' will run before built-in cleanup."
        else
            log "ERROR: Post-command '${POST_COMMAND}' does not exist."
            exit 1
        fi
    fi

    # The runner is a CHILD of this shell here, not an exec over it, because the marker has
    # to be written however the runner ends. Its --post-run hooks run only after a scenario
    # that produced a result; a runner that crashes, is OOM-killed or is torn down by the
    # kubelet runs none of them, and without the marker the uploader never starts. So the
    # EXIT trap repeats the
    # two post-run steps: cleanup first (idempotent -- a daemon already stopped has no
    # pidfile), then the marker, so it is written after this container's files are complete.
    #
    # tini forwards the kubelet's TERM to this shell only, so the shell forwards it to the
    # runner; `wait` returns when the trap has run, and the loop is what reaps.
    _scenario_pid=""
    _finish() {
        "${BUILTIN_CLEANUP_SCRIPT}" || true
        "${MARK_DONE_SCRIPT}" || true
    }
    # `|| true`, because this runs under `set -e`: a TERM that arrives once the runner is
    # gone would otherwise end the shell inside its own trap, with a status that is not
    # the runner's.
    _forward_term() {
        if [ -n "${_scenario_pid}" ]; then kill -TERM "${_scenario_pid}" 2>/dev/null || true; fi
    }
    trap _finish EXIT
    trap _forward_term TERM INT
    # A background job in a shell without job control gets SIGINT and SIGQUIT set to SIG_IGN,
    # and that disposition survives every exec below it: the runner, the scenario's children,
    # a bag recorder. A recorder that ignores SIGINT is killed rather than closed, and a bag
    # that was never closed has no metadata.yaml, which makes it unreadable to every
    # converter -- so the run looks successful and its trajectory is gone. Neither side can
    # undo it later: exec preserves SIG_IGN, and a non-interactive shell may not reset a
    # signal that was ignored on entry.
    #
    # `env --default-signal` restores exactly those two dispositions for the runner and
    # everything under it. Job control does the same by giving the job its own process
    # group, and is the fallback for a coreutils too old for the flag.
    _RUN_WITH_SIGNALS=""
    if env --default-signal=INT,QUIT true 2>/dev/null; then
        _RUN_WITH_SIGNALS="env --default-signal=INT,QUIT"
    else
        set -m
    fi
    run_scenario() {
        # Unquoted, so an empty prefix disappears rather than becoming an argv[0]: the job
        # must stay a simple command, or $! names a subshell and the TERM above is
        # forwarded to something that is not the runner.
        ${_RUN_WITH_SIGNALS} "$@" &
        _scenario_pid=$!
        local _rc=0
        wait "${_scenario_pid}" || _rc=$?
        while kill -0 "${_scenario_pid}" 2>/dev/null; do
            wait "${_scenario_pid}" || _rc=$?
        done
        exit "${_rc}"
    }"""


def job_node_alias(campaign_data) -> str | None:
    """The node alias this campaign's cluster jobs are confined to, or ``None`` for the pool.

    The single reader of ``execution.kubernetes.jobs.node``. The alias narrows the cluster's
    job pool to the one node registered under it; resolving it to that node is the
    Kubernetes backend's job.

    Accepts either the raw mapping or a validated model at each level, matching the two
    shapes callers already pass around.
    """
    def field(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    node = field(field(field(field(campaign_data, "execution"), "kubernetes"), "jobs"), "node")
    return node or None


def check_campaign_inputs(campaign_data):
    """Fail with one actionable error if a required project input is missing.

    Staging copies the ``.vast``, the scenario file and the ``run_files`` verbatim,
    so a wrong path otherwise surfaces as ``shutil``'s ``[Errno 2] ... '<path>'`` —
    which names neither the ``.vast`` key the path came from nor what it was
    resolved against, and arrives mid-campaign, after the campaign dir and the
    store entry already exist. Checked up front and reported together instead, as
    the user error it is (no traceback; see :class:`CampaignConfigError`).
    """
    vast_file = campaign_data.get("vast")
    vast_dir = os.path.dirname(vast_file) if vast_file else ""
    candidates = [("the .vast file", vast_file, vast_file),
                  ("execution.scenario_file", campaign_data.get("scenario_file"),
                   campaign_data.get("scenario_file"))]
    # run_files are collected relative to the .vast's directory; _input_files and the
    # transient files are skipped-with-a-warning at their copy site (they are optional
    # extras, not something a run cannot start without), so they are not checked here.
    for run_file in campaign_data.get("_run_files", []):
        candidates.append(("execution.run_files", run_file,
                           os.path.join(vast_dir, run_file)))
    missing = [entry for entry in candidates
               if not entry[2] or not os.path.isfile(entry[2])]
    if missing:
        raise missing_input_error(missing)


def render_secondary_entrypoint(*, cluster=False) -> str:
    """The sidecar entrypoint as it is shipped to a campaign: the shared blocks substituted."""
    content = files('robovast.execution.data').joinpath('secondary_entrypoint.sh').read_text(
        encoding='utf-8')
    content = content.replace('# @@INIT_BLOCK@@',
                              _CLUSTER_INIT_BLOCK if cluster else _LOCAL_INIT_BLOCK)
    content = content.replace('# @@LOG_BLOCK@@', _LOG_BLOCK)
    return content.replace('# @@ROS_SETUP_BLOCK@@', ROS_SETUP_BLOCK)


def render_entrypoint(*, cluster=False, instance_type_command=None):
    """The container entrypoint script, with its placement-specific blocks substituted.

    The template carries three markers whose content depends on *where* the container
    runs: the init block (``fixuid`` outside a pod, config fetch in-cluster), the post-run
    block (how the runner is started and what runs after it), and the instance-type probe. A
    script rendered for one setting is therefore wrong in the other — which is why a
    campaign's staged ``entrypoint.sh`` must never be reused by something running
    elsewhere, and why container-exec renders its own instead of copying one.

    Separate from :func:`prepare_campaign_configs` because a diagnostic that runs a bare
    command needs the entrypoint *without* a config tree — and demanding a valid
    scenario file to produce one would fail checks that have nothing to do with the
    question being asked.
    """
    entrypoint_src = str(files('robovast.execution.data').joinpath('entrypoint.sh'))
    with open(entrypoint_src, 'r', encoding='utf-8') as f:
        content = f.read()
    init_block = _CLUSTER_INIT_BLOCK if cluster else _LOCAL_INIT_BLOCK
    post_run_block = _CLUSTER_POST_RUN_BLOCK if cluster else _LOCAL_POST_RUN_BLOCK
    content = content.replace('# @@INIT_BLOCK@@', init_block)
    content = content.replace('# @@LOG_BLOCK@@', _LOG_BLOCK)
    content = content.replace('# @@ROS_SETUP_BLOCK@@', ROS_SETUP_BLOCK)
    content = content.replace('# @@INSTANCE_TYPE_BLOCK@@',
                              instance_type_command or _NO_INSTANCE_TYPE)
    content = content.replace('    # @@POST_RUN_BLOCK@@', post_run_block)
    return content


def _record_resolved_plugins(out_dir, vast_dir, campaign_data) -> None:
    """Write ``_execution/plugins.yaml`` for this campaign, if it declares any plugins.

    Kept out of :func:`prepare_campaign_configs` proper because it is provenance, not staging:
    it must never be able to stop a campaign being prepared, and a reader of that function
    should not have to hold plugin metadata in mind.
    """
    try:
        from robovast.common.campaign_data import \
            write_plugins_record  # pylint: disable=import-outside-toplevel
        from robovast.common.config_plugins import (  # pylint: disable=import-outside-toplevel
            resolved_plugin_versions)

        specs = (campaign_data.get("plugins")
                 or (campaign_data.get("vast_config") or {}).get("plugins")
                 or _plugin_specs_of(campaign_data))
        write_plugins_record(out_dir, resolved_plugin_versions(vast_dir, specs))
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Could not record resolved plugin versions: %s", e)


def _plugin_specs_of(campaign_data) -> list:
    """The campaign's top-level ``plugins:`` list, read back from its own ``.vast``.

    ``campaign_data`` is the *composed* result and does not carry the raw top-level block, so
    the authored file is the source. Read as a subsection, which is the lenient policy -- this
    is provenance for a campaign already being prepared, and refusing over an unrelated part
    of the file would take the campaign down to record a note about it.
    """
    from robovast.common.common import load_config  # pylint: disable=import-outside-toplevel

    vast_path = campaign_data.get("vast")
    if not vast_path:
        return []
    section = load_config(vast_path, subsection="plugins", allow_missing=True)
    return section if isinstance(section, list) else []



def _copy_into(src, dst) -> None:
    """Copy *src* to *dst*, unless they are already the same file.

    A campaign re-entered after a service restart is relaunched from its own frozen
    ``_config/``, so the project tree and the campaign tree are one directory and each copy
    below is a file onto itself -- which ``shutil.copy2`` refuses outright
    (``SameFileError``), failing a campaign whose Jobs are still running. Copying the
    project into the campaign is what makes the campaign self-contained; where it already
    is the campaign, that is done.
    """
    if os.path.exists(dst) and os.path.samefile(src, dst):
        return
    shutil.copy2(src, dst)


def snapshot_files(campaign_data, vast_dir) -> list:
    """Every project-relative file ``<campaign>/_config/`` must hold beyond the ``.vast``
    and the scenario: what composition reads, so the campaign can be composed again from it.

    That is ``_run_files`` plus the config files the ``sut:`` channel declares. The two
    differ only in what a run sees: a run mounts ``_run_files``, and never a declared source
    -- it gets its configuration's rewritten copy instead, which composition writes from the
    archived original.
    """
    run_files = list(campaign_data.get("_run_files") or [])
    sources = [rel for rel in sut_source_paths(campaign_data.get("execution") or {}, vast_dir)
               if rel not in run_files]
    return run_files + sources


def _archive_vast_sources(vast_src, campaign_config_dir):
    """Copy the campaign's ``.vast`` and every base it extends into ``_config/``.

    Returns the archived path of the campaign's own file. Sources keep their position relative
    to the project directory, which is what makes the archive a tree the author would
    recognise rather than a flat pile whose ``extends:`` no longer resolve.
    """
    from robovast.common.config_extends import \
        extends_sources  # pylint: disable=import-outside-toplevel

    # Imported here: results_utils imports this module, so the pair can only meet at call time.
    from robovast.common.results_utils import \
        write_campaign_pointer  # pylint: disable=import-outside-toplevel

    # The RAW document, not `load_config`: that resolves `extends:` and removes it, so the
    # chain this has to walk would already be gone.
    with open(vast_src, "r", encoding="utf-8") as handle:
        raw = next(iter(yaml.safe_load_all(handle)), None) or {}
    project_dir = os.path.abspath(os.path.dirname(vast_src))
    own_dst = None
    for src in extends_sources(raw, vast_src):
        rel = os.path.relpath(str(src), project_dir)
        dst = os.path.join(campaign_config_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _copy_into(str(src), dst)
        own_dst = dst
    write_campaign_pointer(campaign_config_dir, own_dst)
    return own_dst


def prepare_campaign_configs(out_dir, campaign_data, cluster=False,
                             instance_type_command=None):
    """Stage a campaign's config tree, including the generated entrypoint.

    *instance_type_command* is a shell line that sets ``INSTANCE_TYPE``, obtained from the
    cluster provider's
    :meth:`~robovast.execution.cluster_config.base_config.BaseConfig.get_instance_type_command`
    — the machine type on a cloud (GCP's metadata server, Azure's IMDS), the architecture
    on bare metal. The *caller* resolves it rather than this function looking a provider
    up, so ``robovast.common`` keeps no dependency on the cluster packages. Omitted, the
    recorded instance type is empty, which is the honest answer where nothing can say.
    """
    # Create the output directory structure
    logger.debug(f"Campaign Configs: {pformat(campaign_data)}")
    check_campaign_inputs(campaign_data)
    os.makedirs(out_dir, exist_ok=True)

    campaign_config_dir = os.path.join(out_dir, "_config")
    os.makedirs(campaign_config_dir, exist_ok=True)

    campaign_transient_dir = os.path.join(out_dir, "_transient")
    os.makedirs(campaign_transient_dir, exist_ok=True)

    entrypoint_dst = os.path.join(campaign_transient_dir, "entrypoint.sh")
    with open(entrypoint_dst, 'w', encoding='utf-8') as f:
        f.write(render_entrypoint(cluster=cluster,
                                 instance_type_command=instance_type_command))

    secondary_entrypoint_dst = os.path.join(campaign_transient_dir, "secondary_entrypoint.sh")
    with open(secondary_entrypoint_dst, 'w', encoding='utf-8') as f:
        f.write(render_secondary_entrypoint(cluster=cluster))

    # Copy collect_sysinfo.py into _transient/
    collect_sysinfo_src = str(files('robovast.execution.data').joinpath('collect_sysinfo.py'))
    collect_sysinfo_dst = os.path.join(campaign_transient_dir, "collect_sysinfo.py")
    shutil.copy2(collect_sysinfo_src, collect_sysinfo_dst)

    # Copy the resource monitor into _transient/, from where it is mounted at /config in
    # every container.
    monitor_src = str(files('robovast.execution.data').joinpath('monitor_resources.py'))
    shutil.copy2(monitor_src, os.path.join(campaign_transient_dir, 'monitor_resources.py'))

    # Copy rosbag processing scripts into _transient/ for host-side post-run processing
    for script_name in ('rosbags_process.py', 'rosbags_common.py', 'ros2_exec.sh'):
        src = str(files('robovast.results_processing.data').joinpath(script_name))
        shutil.copy2(src, os.path.join(campaign_transient_dir, script_name))
    os.chmod(os.path.join(campaign_transient_dir, 'ros2_exec.sh'), 0o755)

    vast_file_path = os.path.dirname(campaign_data["vast"])

    # Prepare campaign_data for configurations.yaml (strip internal keys)
    campaign_data_for_dump = copy.deepcopy(campaign_data)
    campaign_data_for_dump.pop("_transient_files", None)
    campaign_data_for_dump.pop("_output_dir", None)
    for c in campaign_data_for_dump.get("configs", []):
        c.pop("_config_block", None)

    # Save scenario variations as YAML in _transient subdirectory
    scenario_variations_path = os.path.join(campaign_transient_dir, "configurations.yaml")
    with open(scenario_variations_path, 'w') as f:
        yaml.dump(convert_dataclasses_to_dict(campaign_data_for_dump), f, default_flow_style=False, sort_keys=False)
    logger.debug(f"Saved configurations to {scenario_variations_path}")

    # Compute hashes once per run (reused for all configs)
    run_files_hash = hash_run_files(vast_file_path, campaign_data.get("_run_files", []))
    # The config files the `sut:` channel addresses are generation inputs too, and cannot
    # ride in run_files_hash -- that list is also staged, and staging a source beside its
    # rewritten copy is exactly what the channel refuses.
    sut_sources_hash = hash_run_files(
        vast_file_path,
        sut_source_paths(campaign_data.get("execution", {}) or {}, vast_file_path))
    # Config generation already resolved this against the .vast's location, so it is usable as-is
    # Re-prepending the .vast's directory doubled it -- e.g.
    # `<project>/<project>/scenario.osc` -- for every project whose config path has a
    # directory part, and was a silent no-op only for the usual case of a `.vast` sitting in
    # the project's own directory.
    scenario_file_path_for_hash = campaign_data["scenario_file"]
    scenario_file_hash = (
        hash_file_content(scenario_file_path_for_hash)
        if os.path.isfile(scenario_file_path_for_hash)
        else ""
    )

    # Copy scenario_file into _config/
    scenario_rel = os.path.basename(campaign_data["scenario_file"])
    scenario_config_dst = os.path.join(campaign_config_dir, scenario_rel)
    os.makedirs(os.path.dirname(scenario_config_dst), exist_ok=True)
    _copy_into(scenario_file_path_for_hash, scenario_config_dst)

    # Copy the .vast file into _config/, with whatever it is built on.
    #
    # `_config/` mirrors the project tree, so a campaign extending a base archives both at the
    # paths the author gave them and `reconstruct_project` hands back a tree that still says
    # what it said. Which of those files is the campaign is a fact known here and not
    # recoverable by looking afterwards -- alphabetical order has no reason to favour it --
    # so it is recorded rather than re-derived.
    #
    # A campaign extending nothing takes the same path and copies one file, byte for byte,
    # comments and anchors intact.
    vast_src = campaign_data["vast"]
    _archive_vast_sources(vast_src, campaign_config_dir)

    # What the declared plugin specs resolved to. Recorded HERE because this is where the
    # .vast directory -- and so its .robovast_plugins/ install dir -- is in hand; the
    # execution.yaml writers run later and from places that have neither. A `plugins:` entry
    # is usually not a pin ("pkg @ git+...@main"), and a hash of the specs -- the only other
    # thing available here -- is identical across every resolution of them.
    _record_resolved_plugins(out_dir, vast_file_path, campaign_data)

    # Copy what composition reads, so the campaign can be composed again from _config/
    for config_file in snapshot_files(campaign_data, vast_file_path):
        src_path = os.path.join(vast_file_path, config_file)
        dst_path = os.path.join(campaign_config_dir, config_file)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        _copy_into(src_path, dst_path)

    # Copy variation input files and analysis notebooks into _config/
    for input_file in campaign_data.get("_input_files", []):
        src_path = os.path.join(vast_file_path, input_file)
        dst_path = os.path.join(campaign_config_dir, input_file)
        if not os.path.exists(src_path):
            # Skipped rather than fatal, deliberately: `retrigger.missing_run_files`
            # tolerates the same absence so that re-running a campaign that was already
            # short a notebook still works. `validate_project` is the gate that refuses a
            # declared notebook before any compute is spent -- so what this line owes the
            # reader is what BREAKS, since the campaign will otherwise finish looking fine
            # and the Explorer tab will only fail when someone opens the results.
            if input_file.endswith(".ipynb"):
                logger.warning(
                    "Analysis notebook not found, skipping: %s. It is declared under "
                    "visualization.results.explorer.notebooks, so its Explorer tab will "
                    "fail to render for this campaign. Run 'vast config validate' on the "
                    "project to catch this before starting one.", src_path)
            else:
                logger.warning(f"Input file not found, skipping: {src_path}")
            continue
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        _copy_into(src_path, dst_path)

    # Copy campaign-level transient files into _transient/
    for rel_path, abs_path in campaign_data.get("_transient_files", []):
        if not os.path.exists(abs_path):
            logger.warning(f"Transient file not found, skipping: {abs_path}")
            continue
        dst_path = os.path.join(campaign_transient_dir, rel_path)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        _copy_into(abs_path, dst_path)

    # get scenario name
    original_scenario_path = campaign_data.get("scenario_file")
    try:
        scenario_params = get_scenario_parameters(original_scenario_path)
        scenario_name = next(iter(scenario_params.keys()))

        if scenario_name is None:
            raise ValueError(f"Scenario name not found in {original_scenario_path}")
    except Exception as e:
        raise RuntimeError(f"Could not get scenario name from {original_scenario_path}: {e}") from e

    for config_data in campaign_data["configs"]:
        run_config_dir = os.path.join(out_dir, config_data.get("name"), "_config")

        # Compute and write config identifier for merge-campaigns
        config_block = config_data.get("_config_block", {})
        variation_type_names = [
            v["name"] for v in config_data.get("_variations", [])
        ]
        config_identifier, sub_identifier = compute_config_identifier(
            vast_file_path,
            config_block,
            run_files_hash,
            scenario_file_hash,
            variation_type_names,
            sut_sources_hash,
        )
        config_yaml_path = os.path.join(run_config_dir, "config.yaml")
        os.makedirs(run_config_dir, exist_ok=True)
        with open(config_yaml_path, "w") as f:
            yaml.dump(
                {"config_identifier": config_identifier, "sub_identifier": sub_identifier},
                f,
                default_flow_style=False,
                sort_keys=False,
            )

        # Copy config files
        # artifact paths may be relative to campaign_data["_output_dir"]; source
        # paths are always absolute.
        _gen_output_dir = campaign_data.get("_output_dir", "")
        if "_config_files" in config_data:
            for config_rel_path, config_path in config_data["_config_files"]:
                src_path = (
                    config_path
                    if os.path.isabs(config_path)
                    else os.path.join(_gen_output_dir, config_path)
                )
                if not os.path.exists(src_path):
                    # A generated artifact, not something the user wrote: the message
                    # has to say so, or the path reads like a bad .vast entry. The
                    # usual cause is a config-generation cache entry whose artifact
                    # tarball no longer matches, so name the cache as the remedy.
                    raise CampaignConfigError(
                        f"Config '{config_data.get('name')}': the generated config "
                        f"file '{config_rel_path}' is missing at {src_path}.\n"
                        "It is produced by config generation, not by the .vast — a "
                        "stale generation cache is the usual cause. Remove "
                        f"{os.path.join(vast_file_path, '.cache')} and retry.")
                dst_path = os.path.join(run_config_dir, config_rel_path)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                _copy_into(src_path, dst_path)

        # Copy config-level transient files into <config>/_transient/
        config_name = config_data.get("name", "")
        for rel_path, path in config_data.get("_config_transient_files", []):
            abs_path = (
                path
                if os.path.isabs(path)
                else os.path.join(_gen_output_dir, path)
            )
            if not os.path.exists(abs_path):
                logger.warning(f"Config transient file not found, skipping: {abs_path}")
                continue
            dst_path = os.path.join(out_dir, config_name, "_transient", rel_path)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            _copy_into(abs_path, dst_path)

        # The simulation channel's record, beside the scenario channel's. A record, not an
        # input: what the run reads is the per-job overrides file plus the world on argv.
        # It is here so that what the simulator was given is a file next to the
        # configuration it belongs to -- greppable, diffable, and directly replayable, its
        # two values being the arguments that reproduce the cell by hand.
        #
        # Written for every configuration that HAS one: a campaign with no simulator has
        # nothing to record, which is what the guard below enforces.
        sim_block = config_data.get("sim")
        if sim_block:
            os.makedirs(run_config_dir, exist_ok=True)
            with open(os.path.join(run_config_dir, SIM_CONFIG_FILE), "w") as f:
                yaml.dump(convert_dataclasses_to_dict(copy.deepcopy(sim_block)), f,
                          default_flow_style=False, sort_keys=False)

        # The same, for how the system under test is configured. The rewritten files are
        # what the cell runs; this is what a reader consults to see which factor produced
        # which value, without diffing two copies of a stack's config.
        sut_block = config_data.get("sut")
        if sut_block:
            os.makedirs(run_config_dir, exist_ok=True)
            with open(os.path.join(run_config_dir, SUT_CONFIG_FILE), "w") as f:
                yaml.dump(convert_dataclasses_to_dict(copy.deepcopy(sut_block)), f,
                          default_flow_style=False, sort_keys=False)

        # Create config file if needed
        if "config" in config_data:
            config = config_data.get('config')
            if config is not None:
                config_dict = convert_dataclasses_to_dict(copy.deepcopy(config))
                wrapped_config_data = {scenario_name: config_dict}
                dst_path = os.path.join(run_config_dir, 'scenario.config')
                os.makedirs(run_config_dir, exist_ok=True)
                with open(dst_path, 'w') as f:
                    yaml.dump(wrapped_config_data, f, default_flow_style=False, sort_keys=False)


def build_job_parameter_documents(job, scenario_name):
    """Build scenario-parameter override documents for a packed job.

    Produces one YAML document per work item in the job. Each document
    overrides ``scenario_name``'s parameters for that config and sets the special
    ``_output_dir`` key to ``<config-name>/<run_number>`` so scenario_execution
    writes the item's results into robovast's per-config/run layout.

    A file-valued parameter is carried **as the campaign wrote it**. It needs no
    rewriting: a scenario resolves a file parameter against its own directory, which is
    the config mount, and a configuration's own copy of a file is staged there in place of
    the campaign's -- so the path the campaign wrote already names the cell's file.

    Args:
        job: A :class:`~robovast.execution.packer.JobSpec`.
        scenario_name: The scenario name to override (top-level key, matching
            the single-config ``scenario.config`` wrapping).

    Returns:
        list[dict]: One override document per work item, ready to dump as a
        multi-document YAML for ``--scenario-parameter-file``.
    """
    documents = []
    for item in job.items:
        config_data = item.config
        config_name = config_data.get("name", "")
        config = config_data.get("config") or {}
        config_dict = convert_dataclasses_to_dict(copy.deepcopy(config))

        # _output_dir is consumed by scenario_execution to place this item's
        # results; relative paths resolve under -o/--output-dir.
        config_dict["_output_dir"] = f"{config_name}/{item.run_number}"
        documents.append({scenario_name: config_dict})
    return documents


def dump_multi_document_yaml(documents) -> str:
    """Serialise a list of dicts as a multi-document YAML string (``---`` separated)."""
    return yaml.dump_all(documents, default_flow_style=False, sort_keys=False)


# Filename of the per-campaign job-link manifest written into ``_transient/``.
JOB_LINKS_MANIFEST = "job_links.yaml"


#: Names the run itself owns at the config mount, and which a campaign's own files
#: therefore may not occupy.
#:
#: Every one of these is written into the campaign's ``_config/`` or ``_transient/`` and
#: reaches the container at ``/config/<name>`` -- the entrypoint the container executes, the
#: scripts it sources, the parameter documents it reads. A configuration's file is staged
#: where the campaign's copy would have been, so a deploy path equal to one of these would
#: land on it. Refused at composition rather than left to the cluster, which discovers it as a
#: refused mount or a pod that dies in its entrypoint, after the image pull.
#:
#: An allowlist of what the run owns, not a guess at what a campaign might write: the set is
#: exactly what :func:`prepare_campaign_configs` and the backend put there, and it lives
#: beside the code that writes it so the two cannot drift.
RESERVED_CONFIG_MOUNT_NAMES = frozenset({
    "entrypoint.sh", "secondary_entrypoint.sh",
    "collect_sysinfo.py", "monitor_resources.py",
    "rosbags_process.py", "rosbags_common.py", "ros2_exec.sh",
    "configurations.yaml", JOB_LINKS_MANIFEST,
    "scenario.config", "scenario.params.yaml",
    os.path.basename(SIM_OVERRIDES_MOUNT),
})

#: Per-job documents at the config mount, which carry a job tag rather than a fixed name.
#: Matched as patterns for the same reason the set above is matched by name.
RESERVED_CONFIG_MOUNT_PATTERNS = tuple("*" + suffix for suffix in JOB_DOCUMENT_SUFFIXES)


def job_artifact_rel(index, job_prefix="") -> str:
    """Path of job *index*'s artifact dir under ``_jobs/`` (no leading ``_jobs/``).

    Nested ``<prefix>/job-<idx>`` when the run is batched, else flat ``job-<idx>``.
    Both backends lay ``_jobs/`` out this way, so this is the one definition of that
    layout — the local runner, the cluster runner, and every reader resolve through it.
    """
    return f"{job_prefix}/job-{index}" if job_prefix else f"job-{index}"


def build_job_links(jobs, job_prefix="") -> dict:
    """Map each work item's ``job`` link to its job's artifact directory.

    For a packed job ``N`` running config ``C`` at run ``R``, the work item's
    result dir is ``C/R`` and the job-level artifacts (sysinfo, logs, resource
    monitor) live in ``_jobs[/<prefix>]/job-N``. This returns a ``{link: target}``
    mapping where the link is ``C/R/job`` and the target is that dir relative to the
    link's directory, so a user can ``cd C/R/job`` to reach the job's artifacts.

    Args:
        jobs: An iterable of :class:`~robovast.execution.packer.JobSpec`.
        job_prefix: Batch namespace (e.g. ``"batch-3"``) when runs are executed in
            batches; empty for the flat single-batch layout. Must match the prefix the
            runner actually writes under, or the manifest points at a dir that
            never exists.

    Returns:
        dict[str, str]: ``{"<config>/<run>/job": "../../_jobs[/<prefix>]/job-<idx>"}``.
    """
    links = {}
    for job in jobs:
        target = f"../../_jobs/{job_artifact_rel(job.index, job_prefix)}"
        for item in job.items:
            links[f"{item.config_name}/{item.run_number}/job"] = target
    return links


def write_job_links_manifest(transient_dir, jobs, job_prefix="", *, base=None) -> None:
    """Write the ``job_links.yaml`` manifest (link → relative target) for *jobs*.

    No-op when there are no links (e.g. single-config jobs have no ``_jobs``
    split). The manifest is plain data, so it survives an S3 round-trip and is
    consumed where results are materialised (the results root and the share archiver).

    *base* is what this manifest must keep: the links the campaign already has, from
    :func:`read_job_links`. **The manifest is campaign-level while it is written per batch**,
    so a batch that publishes only its own links leaves every earlier batch's runs
    unresolvable — no ``run_log``, no ``resource_usage``, reported only as "no job_links
    entry". Pass it whenever more batches may follow.

    It is accumulated **only when** *job_prefix* namespaces the target, and that is not a
    detail. Unprefixed, a target is ``_jobs/job-<idx>``, an index meaningful only within the
    call that assigned it: re-running a campaign, or packing it differently, moves ``cfg/1``
    from ``job-1`` to ``job-0``, and keeping the older entry would aim a run at another run's
    artifacts. Prefixed (``_jobs/batch-3/job-0``) it is stable for the life of the campaign,
    and accumulating is then not optional but required. So an unprefixed write replaces,
    which is also what a single-batch campaign — one call, the default — has always done.

    Within a prefixed accumulation an entry whose target would *change* raises: one run's
    artifacts cannot live in two jobs, and picking a winner silently is how a run ends up
    resolving to another batch's log.
    """
    links = dict(base or {}) if job_prefix else {}
    for link, target in build_job_links(jobs, job_prefix).items():
        previous = links.get(link)
        if previous is not None and previous != target:
            raise ValueError(
                f"conflicting {JOB_LINKS_MANIFEST} entry for {link!r}: "
                f"already {previous!r}, now {target!r}")
        links[link] = target
    if not links:
        return
    os.makedirs(transient_dir, exist_ok=True)
    with open(os.path.join(transient_dir, JOB_LINKS_MANIFEST), "w") as f:
        yaml.dump(links, f, default_flow_style=False, sort_keys=True)


#: The job-link manifest's location inside a campaign, as one path rather than two joins.
#: A reader that has the campaign's bytes rather than its directory -- the cluster's
#: object store -- needs the same address, and deriving it twice is how the two drift.
JOB_LINKS_MANIFEST_REL = os.path.join("_transient", JOB_LINKS_MANIFEST)


def resolve_job_artifact_rel(links: dict, job_name: str) -> str:
    """``<config>/<run>`` -> its job-artifact dir, relative to the campaign root.

    The path arithmetic of :func:`job_artifact_dir` without the filesystem: the manifest's
    target is relative to the link's own directory, so it only becomes a campaign-relative
    path after being joined with *job_name* and normalised. Split out because the cluster
    resolves the same job against an object-store prefix, where there is no directory
    to join against and re-deriving the arithmetic would let two readers disagree about
    which job a run's artifacts are in.

    Raises:
        FileNotFoundError: When *links* has no entry for *job_name* — the artifacts are
            unlocatable, which must not be reported as "no output".
    """
    target = links.get(f"{job_name}/job")
    if not target:
        raise FileNotFoundError(
            f"no {JOB_LINKS_MANIFEST} entry for {job_name!r}")
    return os.path.normpath(os.path.join(job_name, target))


def read_job_links(campaign_dir) -> dict:
    """Load a campaign's ``{link: target}`` job-link manifest ({} when absent)."""
    manifest = os.path.join(campaign_dir, "_transient", JOB_LINKS_MANIFEST)
    if not os.path.isfile(manifest):
        return {}
    with open(manifest) as f:
        return yaml.safe_load(f) or {}


def job_artifact_dir(campaign_dir, job_name, links=None) -> str:
    """Resolve ``<config>/<run>``'s job-artifact dir (logs, sysinfo, resource monitor).

    Those artifacts are written per JOB under ``_jobs/``, never into the run dir, so
    ``<config>/<run>/logs/`` stays empty and reading there yields a silently blank log.

    Resolution goes through the manifest, not the ``job`` symlink: the manifest is
    written before the first job starts, while the symlink is only created once a job
    finishes, so a RUNNING job resolves through the manifest alone. The symlink stays
    the user-facing affordance (``cd C/R/job``); the manifest is the machine-readable
    source of truth, and both come from :func:`build_job_links`.

    Args:
        campaign_dir: The campaign's results directory.
        job_name: ``"<config>/<run>"``.
        links: The campaign's manifest as :func:`read_job_links` returns it. A caller
            resolving every run of a campaign passes it: the manifest holds one entry per
            run, so reading it per call makes the walk quadratic in the run count. Omitted,
            it is read here.

    Returns:
        str: Path to the job's artifact directory, relative to *campaign_dir*'s root
        in the same sense *campaign_dir* itself is.

    Raises:
        FileNotFoundError: When the campaign has no manifest entry for *job_name* —
            the job's artifacts are unlocatable, which must not be reported as
            "no output".
    """
    if links is None:
        links = read_job_links(campaign_dir)
    try:
        rel = resolve_job_artifact_rel(links, job_name)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"{e} in {campaign_dir!r}") from None
    return os.path.normpath(os.path.join(campaign_dir, rel))


def create_job_links(campaign_dir) -> int:
    """Create the ``job`` symlinks described by a campaign's link manifest.

    Reads ``<campaign_dir>/_transient/job_links.yaml`` and creates each
    ``<config>/<run>/job`` relative symlink pointing at its job's artifact dir.
    Idempotent: an existing ``job`` entry is replaced. Missing manifest is a
    no-op (single-config campaigns have none). Returns the number of links
    created.
    """
    links = read_job_links(campaign_dir)
    created = 0
    for link_rel, target in links.items():
        link_path = os.path.join(campaign_dir, link_rel)
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        # Replace any existing entry so re-runs are idempotent.
        if os.path.islink(link_path) or os.path.exists(link_path):
            try:
                os.remove(link_path)
            except OSError:
                pass
        os.symlink(target, link_path)
        created += 1
    return created


#: Fields of ``execution.yaml`` that describe the CAMPAIGN's images rather than one execution
#: of it, and are therefore carried forward when a rewrite has nothing to put in them.
#:
#: The distinction is the whole point, so it is a list and not "everything missing": a digest is
#: a fact about the campaign and stays true, while ``execution_time``, ``runs``, ``cluster_info``
#: and the ``robovast_*`` provenance describe the run that is writing right now and MUST be
#: overwritten -- carrying those forward would mix two executions into one record and misreport
#: which code, and which cluster, produced the campaign.
_CARRIED_PROVENANCE_FIELDS = ('image', 'images', 'image_revision', 'image_revisions',
                              'image_build_refs')


def _records_nothing(value) -> bool:
    """Whether a provenance field holds no actual fact.

    ``'unknown'`` is the placeholder written when no digest was known, so it counts as nothing
    and never overwrites a recorded digest.
    """
    return not value or value == 'unknown'


def _carry_forward_provenance(path, execution_data: dict) -> None:
    """Keep image provenance a rewrite cannot re-derive, instead of blanking it.

    ``execution.yaml`` is rewritten, not appended to, and a rewrite can know strictly less than
    the one before it: a resume starts after its campaign's pods have been reaped, so the
    per-container digests it would read are gone. Emitting those keys only when there is
    something to put in them would make the rewrite *delete* what the first run recorded,
    leaving a campaign with ``image_revision`` present, ``image_revisions`` absent, and no way
    to re-run it.

    Best-effort and never fatal: an unreadable previous record leaves the new one exactly as
    composed. Only *missing or empty* fields are filled, so a rewrite that does know better
    always wins.
    """
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            previous = yaml.safe_load(handle) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return
    if not isinstance(previous, dict):
        return
    for field in _CARRIED_PROVENANCE_FIELDS:
        if _records_nothing(execution_data.get(field)) and not _records_nothing(
                previous.get(field)):
            execution_data[field] = previous[field]
            logger.info("Kept %s from the previous execution record: this write had none, "
                        "and it is a fact about the campaign rather than about this run.",
                        field)


def create_execution_yaml(runs, output_dir, execution_params=None, context=None,
                          image_digest=None, image_digests=None, image_labels=None,
                          nodes_skipped=None):
    """Create execution.yaml file with ISO formatted timestamp.

    Args:
        runs: Number of runs to include in execution.yaml
        output_dir: Directory where execution.yaml will be created
        execution_params: Dictionary containing execution parameters (run_as_user, env, etc.)
        context: Kubernetes context name to use. ``None`` uses the active context.
        image_labels: ``{role: {label: value}}`` already read for each container's image, when
            the caller has them. The service does -- it reads them from the registry to
            check the container protocol -- and must pass them, because this function runs in
            the controller pod where the docker probes it would otherwise use cannot work.
        image_digest: The immutable ``repo@sha256:…`` the run pods actually used, when
            known (see ``KubernetesBackend._capture_image_digest``). Recorded as
            ``image_revision`` so a floating ``:latest`` is pinned to the exact image the
            runs ran — and postprocessing reuses it (``campaign_execution_image``). Falls
            back to the docker daemon's image id (``unknown`` without one) when None.
    """
    if execution_params is None:
        execution_params = {}

    execution_dir = os.path.join(output_dir, "_execution")
    os.makedirs(execution_dir, exist_ok=True)
    execution_yaml_path = os.path.join(execution_dir, "execution.yaml")
    execution_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 'image' stays the container the scenario ran in -- postprocessing and the store
    # read it as "the campaign's image". 'images' records every container, because with
    # a simulator or a system under test in its own container that single field no
    # longer describes what ran.
    # Imported here, not at module scope: this module is kept free of ``config``
    # imports to avoid a cycle (see BUILD_IMAGE_PREFIX above, duplicated for the same
    # reason). One name is not worth duplicating a third time.
    from robovast.common.config import SCENARIO_CONTAINER  # pylint: disable=import-outside-toplevel
    containers = execution_params.get('containers') or {}
    images = {name: (block or {}).get('image')
              for name, block in containers.items() if (block or {}).get('image')}
    image = images.get(SCENARIO_CONTAINER)
    execution_data = {
        'execution_time': execution_time,
        'robovast_version': get_app_version(),
        # The full sha, the dirty flag and the changed paths -- what a re-run a year from now
        # needs and what `robovast_version` cannot give: it resolves to a semver whenever the
        # git lookup fails, so it can read as an answer while carrying no revision at all.
        **{f'robovast_{key}': value for key, value in campaign_code_provenance().items()},
        'runs': runs,
        'execution_type': 'cluster',
        'image': image,
        'images': images,
        'image_revision': image_digest or 'unknown',
    }
    # One digest per container, because "the campaign's image" stopped being a single
    # fact. `image_revision` is the scenario container's; anything asking which bytes
    # produced a particular artifact has to name the role. The run view's geometry is the
    # case in hand: it is compiled from the world the capture names, and that world and
    # its exporter live in the SIMULATION image, not the scenario one.
    if image_digests:
        execution_data['image_revisions'] = dict(image_digests)

    # Which machines this campaign did NOT use, and why. Here rather than only in the log
    # because it is a fact about what the campaign ran on: a reader comparing two campaigns
    # has to be able to see that one of them was short a node, and a log line is gone by then.
    # Absent means none was left out, which is the normal case.
    if nodes_skipped:
        execution_data['nodes_skipped'] = dict(nodes_skipped)

    # What each image was built FROM, as opposed to which bytes it is. A digest is reproducible
    # only for as long as the registry keeps it; this is what a rebuild would start from.
    build_refs = image_build_refs(containers, labels_by_role=image_labels)
    if build_refs:
        execution_data['image_build_refs'] = build_refs

    # Add run_as_user if provided
    run_as_user = execution_params.get('run_as_user')
    if run_as_user is not None:
        execution_data['run_as_user'] = run_as_user

    # Add env if provided
    env = execution_params.get('env')
    if env:
        # Convert list of dicts to a single dict for cleaner YAML output
        env_dict = {}
        for env_item in env:
            if isinstance(env_item, dict):
                env_dict.update(env_item)
        if env_dict:
            execution_data['env'] = env_dict

    # Attach cluster information (node count, labels, and cluster config)
    cluster_info = _get_cluster_info(context=context)
    if cluster_info is not None:
        execution_data['cluster_info'] = cluster_info

    _carry_forward_provenance(execution_yaml_path, execution_data)

    with open(execution_yaml_path, 'w') as f:
        yaml.dump(execution_data, f, default_flow_style=False, sort_keys=False)

    logger.debug(f"Created execution.yaml with timestamp: {execution_time}")
