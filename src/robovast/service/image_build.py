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

"""Experiment-image builds from a project's ``build:`` section.

This module is backend-agnostic: it turns a validated project's ``build:`` section
into a deterministic Dockerfile + a content hash, and classifies builder failures
into the structured :class:`~robovast.service.interface.ImageBuildError`. The local
Docker path (``docker buildx build --load``) lives in :class:`LocalImageBuildManager`
here; the in-cluster Job path lives in ``cluster_service`` and reuses the pure
helpers (``build_hash``, ``generate_dockerfile``, ``classify_build_error``).

Registry invariant: nothing here emits or accepts a registry endpoint, credential,
or registry-qualified ref. The agent-facing image is always the symbolic
``build:<tag>``; concrete refs are formed by the backend (a local docker tag here,
a ``<registry_prefix>/<tag>:<hash>`` on the cluster) and never returned to a client.
"""

import hashlib
import logging
import os
import re
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from robovast.common.build_context import BUILD_CONTEXT_IGNORE, render_dockerignore
from robovast.common.containers import plan_containers
from robovast.common.execution import (BUILD_IMAGE_PREFIX, DEFAULT_IMAGE_USER, build_image_tag,
                                       is_build_image_ref, resolve_build_base_image)
from robovast.service.interface import ImageBuildError, ImageBuildRef, ImageBuildStatus, LogChunk

logger = logging.getLogger(__name__)

#: Where the project dir is COPYed inside the image build context.
_CONTEXT_DIR = "/robovast_build_context"
#: Local docker tag namespace for agent-built experiment images.
_LOCAL_TAG_NS = "robovast-build"
#: The buildx builder to build experiment images with. Pinned rather than inheriting whichever
#: builder the operator has selected, because only the ``docker`` driver runs inside dockerd and so
#: shares the daemon's image store and build cache. On a ``docker-container`` builder (a multi-arch
#: one is a common thing to have selected) the base image is invisible and gets re-pulled from the
#: registry on *every* build -- measured at 124 s of a 188 s build for a 5.6 GB ROS base -- the layer
#: cache lives in a separate store that the daemon's images cannot seed, and a locally built
#: ``build.base_image`` (which ``_base_ref`` explicitly supports) cannot be resolved at all.
_LOCAL_BUILDER = "default"
#: BuildKit frontend pin — required for the ``RUN --mount=type=cache`` below. Honoured by both
#: builders we drive: ``docker buildx`` locally and ``buildctl --frontend dockerfile.v0`` in-cluster.
_SYNTAX_DIRECTIVE = "# syntax=docker/dockerfile:1"
#: Prefix the experiment's packages are installed into. ``/usr/local`` and not a fresh directory:
#: it is where pip on a Debian base already installs, so ``PATH``, ``share/ament_index`` and every
#: ``AMENT_PREFIX_PATH`` a project may already set keep pointing at the same place. Only the python
#: package dir moves (``site-packages`` rather than ``dist-packages``), which ``_VENV_SETUP`` bridges.
_VENV = "/usr/local"
#: Make the prefix a venv, and make it visible to the system interpreter again.
#:
#: **Why a venv at all.** The base's numpy/scipy are Debian-packaged and carry no ``RECORD``, so pip
#: cannot uninstall them: any dependency resolving to a different version killed the build minutes in
#: ("Cannot uninstall numpy 1.26.4, RECORD file not found"), and pinning only moved the error from
#: numpy to scipy. Inside a venv pip declines to touch what lives outside it -- "Not uninstalling
#: numpy at /usr/lib/python3/dist-packages, outside environment /usr/local" -- and installs its own
#: copy, which the path order then shadows. The whole failure class goes away.
#:
#: **Why the .pth.** A venv is invisible to /usr/bin/python3, the interpreter ``ros2 launch`` starts
#: nodes with. The one-line ``.pth`` in the system's own site dir hands the venv back to it via
#: ``addsitedir`` (which also processes the nested ``.pth`` files an editable install writes), and
#: lands ahead of Debian's ``dist-packages`` -- restoring exactly the precedence ``/usr/local``
#: already had before it became a venv. Without it the image builds green and the *run* fails on
#: import, so this line is not decoration.
#:
#: ``--without-pip`` because the base has no ``python3.12-venv`` (ensurepip fails); the system pip
#: installs into the venv with ``--python`` instead. Both site dirs are asked for rather than spelled
#: out, so the interpreter version is not baked in. Idempotent, so it costs one cached layer on a
#: base that already has it.
_VENV_SETUP = (
    f"RUN test -f {_VENV}/pyvenv.cfg || ( "
    f"python3 -m venv --system-site-packages --without-pip {_VENV} && "
    "printf 'import site; site.addsitedir(\"%s\")\\n' "
    f"\"$({_VENV}/bin/python3 -c 'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])')\" "
    "> \"$(/usr/bin/python3 -c 'import site; print(site.getsitepackages()[0])')"
    "/robovast_venv.pth\" )")
#: pip invocation for the build steps. ``--python`` targets the venv from the system pip, so no
#: ``--break-system-packages``: the venv is not externally managed (PEP 668).
#:
#: The pip download cache lives in a BuildKit cache mount rather than being suppressed with
#: ``--no-cache-dir``: a mount is never committed to a layer, so the image stays the same size while a
#: rebuilt layer stops re-downloading its wheels (a simulator runtime alone is tens of MB).
#: ``sharing=locked``
#: serialises concurrent builds sharing the mount instead of letting them corrupt the cache.
_PIP_INSTALL = ("RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked "
                f"pip --python {_VENV}/bin/python3 install")
#: Files/dirs never hashed or copied into the build context. Sourced from
#: ``common`` so the in-cluster staging path skips exactly the same set (a
#: mismatch would break the context hash — see build_context).
_IGNORE = BUILD_CONTEXT_IGNORE


@dataclass
class BuildSpec:
    """The normalized ``build:`` section for one project."""

    tag: str
    base_image: Optional[str] = None
    system_packages: list = field(default_factory=list)
    #: As authored: each element is a spec, or a list of specs installed together.
    #: Read it through :attr:`install_groups` (what to install, and in how many
    #: passes) or :attr:`python_specs` (every spec, flat).
    python_packages: list = field(default_factory=list)

    @property
    def install_groups(self) -> list:
        """The specs grouped into pip invocations — one group is one resolution pass.

        A list with no nested list is **one** group: pip then sees every local wheel at
        once, so a wheel's dependency on a sibling resolves against the sibling rather
        than against PyPI, and the author never has to derive an install order. Nesting
        is how an author takes that back to choose layer boundaries; a bare string
        beside a nested list is a group of one.
        """
        if not any(isinstance(entry, list) for entry in self.python_packages):
            return [list(self.python_packages)] if self.python_packages else []
        return [list(entry) if isinstance(entry, list) else [entry]
                for entry in self.python_packages]

    @property
    def python_specs(self) -> list:
        """Every spec, flat — for hashing, validation and context classification."""
        return [spec for group in self.install_groups for spec in group]


def extract_build_specs(campaign_config, base_dir=None) -> dict:
    """One :class:`BuildSpec` per container that adds packages, keyed by container name.

    A campaign may build several images -- a system under test, and a scenario or
    simulation container carrying the experiment's own plugins -- so this returns a map
    rather than the single spec the removed ``build:`` section produced. A container
    with no ``system_packages``/``python_packages`` is absent: it runs its image as-is.

    The tag is the container's **name**, not something the author chose. There is one
    image per container per campaign, so the name already identifies it uniquely, and a
    hand-picked tag was only ever a second thing to keep in sync with
    ``execution.image``.
    """
    execution = getattr(campaign_config, "execution", None)
    if execution is None:
        return {}
    containers = getattr(execution, "containers", None) or {}
    # Through the backend FIRST, exactly as the run path does (config_generation), or the
    # two disagree about which container the packages belong to. A stepped simulator
    # folds `simulation` into `scenario`, so a campaign that declares its packages under
    # `simulation` -- the block where it named the backend, and the natural place --
    # would otherwise be built as an image tagged `simulation` while the container that
    # actually runs is `scenario`. Nothing errors: the run just starts from the unbuilt
    # base image, and the campaign's own code is silently absent.
    execution_dict = {
        'mode': getattr(execution, 'mode', None) or 'auto',
        'containers': {
            name: (block if isinstance(block, dict) else block.model_dump())
            for name, block in containers.items()
        },
    }
    from robovast.common.simulators import apply_backend  # pylint: disable=import-outside-toplevel
    execution_dict = apply_backend(execution_dict, base_dir)
    specs = {}
    plan = plan_containers(execution_dict)
    for container in plan.containers:
        if not container.builds:
            continue
        specs[container.name] = BuildSpec(
            tag=container.name,
            base_image=container.image,
            system_packages=list(container.system_packages),
            python_packages=list(container.python_packages),
        )
    return specs


# ---------------------------------------------------------------------------
# python_packages classification (shared vocabulary with top-level ``plugins:``)
# ---------------------------------------------------------------------------

def _is_source_dir(entry: str, project_dir: Path) -> bool:
    p = (project_dir / entry).resolve()
    try:
        p.relative_to(project_dir.resolve())
    except ValueError:
        return False
    return p.is_dir()


def _is_context_wheel(entry: str, project_dir: Path) -> bool:
    if not entry.endswith(".whl"):
        return False
    p = (project_dir / entry).resolve()
    try:
        p.relative_to(project_dir.resolve())
    except ValueError:
        return False
    return p.is_file()


# ---------------------------------------------------------------------------
# Content hashing — idempotency / cache key
# ---------------------------------------------------------------------------

def _hash_dir(h: "hashlib._Hash", root: Path) -> None:
    """Fold a directory's file paths + contents into *h*, deterministically."""
    for path in sorted(root.rglob("*")):
        if any(part in _IGNORE for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).encode()
        h.update(b"\x00")
        h.update(rel)
        h.update(b"\x01")
        try:
            h.update(path.read_bytes())
        except OSError:
            pass


def _hash_wheel(h: "hashlib._Hash", path: Path) -> None:
    """Fold a wheel's *logical* content into *h*, ignoring zip metadata.

    A wheel is a zip, and rebuilding one from unchanged sources still yields different
    bytes: entry order, compression and the embedded mtimes all move (pip stamps each
    member with its source file's mtime, so a branch switch or a fresh clone rewrites
    every timestamp). Hashing the raw file therefore reported "changed" for wheels that
    install byte-identical files, forcing a full rebuild of every layer downstream.

    Folding name + CRC + uncompressed size per member, in name order, keys on what the
    wheel actually installs. CRC32 is weak against a *crafted* collision but these are
    our own build artifacts, not untrusted input, and it is what the zip central
    directory already carries — so this needs no decompression.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                h.update(b"\x00")
                h.update(info.filename.encode())
                h.update(b"\x01")
                h.update(f"{info.CRC:08x}:{info.file_size}".encode())
    except (OSError, zipfile.BadZipFile):
        # Not a readable zip: fall back to the raw bytes rather than silently hashing
        # nothing, which would collide with every other unreadable wheel.
        try:
            h.update(path.read_bytes())
        except OSError:
            pass


def build_hash(spec: BuildSpec, project_dir: Path, base_ref: str) -> str:
    """A stable short hash of everything that affects the built image.

    Inputs: the resolved base image, apt packages, the python_packages specs, and
    the *contents* of every referenced source directory / context wheel. Changing
    any of these changes the hash (a rebuild); anything else (run_files, scenario)
    does not.
    """
    h = hashlib.sha256()
    # v4: the rendered Dockerfile changed again (installs into a venv at /usr/local, one pip pass
    # per install group instead of one per entry). The Dockerfile text is not itself an input --
    # only the epoch makes a robovast upgrade rebuild rather than serve the image the old renderer
    # produced, which would silently keep the old install semantics.
    h.update(b"v4")
    h.update(base_ref.encode())
    for pkg in sorted(spec.system_packages):
        h.update(b"|apt|")
        h.update(pkg.encode())
    for group in spec.install_groups:  # order matters (install order) → not sorted
        # Grouping is part of what gets built -- the same specs in one pass and in two
        # render different layers -- so the boundary is hashed, not just the specs.
        h.update(b"|grp|")
        for entry in group:
            h.update(b"|py|")
            h.update(entry.encode())
            if _is_source_dir(entry, project_dir):
                _hash_dir(h, (project_dir / entry).resolve())
            elif _is_context_wheel(entry, project_dir):
                _hash_wheel(h, (project_dir / entry).resolve())
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Dockerfile generation — one pip pass per install group
# ---------------------------------------------------------------------------

def generate_dockerfile(spec: BuildSpec, project_dir: Path, base_ref: str,
                        base_user: str = DEFAULT_IMAGE_USER) -> str:
    """Render a deterministic Dockerfile from *spec*.

    Each **install group** becomes one ``RUN``, so pip resolves the group's specs
    together (see :attr:`BuildSpec.install_groups`). The base image supplies the
    ROS/nav2 scaffolding *and* ``/etc/robovast_compat_version``, so the result stays
    compat-valid — we never rewrite that marker.

    A robovast base image ends as an unprivileged user, so the build steps run as root
    and *base_user* is restored afterwards, or a cluster pod — which takes its user
    from the image — would run the scenario as root.

    Layer caching drives the rest of the shape. Each entry gets its **own** ``COPY``
    of just the path it installs, before its group's ``RUN``, rather than one
    ``COPY .`` of the whole project up front: a blanket copy makes every unrelated
    file (the ``.vast`` itself, a scenario, a run file) invalidate all the pip layers,
    which for a project carrying large asset packages means reinstalling hundreds of
    MB to pick up a few KB of changed code. A change outside ``python_packages``
    rebuilds nothing. The apt list is sorted to match :func:`build_hash`, so
    reordering the YAML is a cache hit rather than a rebuild.
    """
    lines = [_SYNTAX_DIRECTIVE, f"FROM {base_ref}", "USER root", _VENV_SETUP,
             f"ENV VIRTUAL_ENV={_VENV}",
             # The packages this file installs land under the venv prefix, so this file
             # is what must make their ament resources findable -- including on a base
             # too old to know about the venv. Sourcing the ROS setup prepends and keeps
             # this entry, and a project that still sets the same value by hand is then
             # a no-op rather than a broken launch.
             f"ENV AMENT_PREFIX_PATH={_VENV}"]
    if spec.system_packages:
        pkgs = " ".join(sorted(spec.system_packages))
        lines.append(
            "RUN apt-get update "
            f"&& apt-get install -y --no-install-recommends {pkgs} "
            "&& rm -rf /var/lib/apt/lists/*")
    for group in spec.install_groups:
        args = []
        for entry in group:
            if _is_source_dir(entry, project_dir):
                lines.append(f"COPY {entry} {_CONTEXT_DIR}/{entry}")
                args.append(f"-e {_CONTEXT_DIR}/{entry}")
            elif _is_context_wheel(entry, project_dir):
                lines.append(f"COPY {entry} {_CONTEXT_DIR}/{entry}")
                args.append(f"{_CONTEXT_DIR}/{entry}")
            else:
                # index pin or git URL — reused verbatim from plugins: vocabulary.
                # Nothing to copy, so it never carries a context layer.
                args.append(f"'{entry}'")
        lines.append(f"{_PIP_INSTALL} {' '.join(args)}")
    lines.append(f"USER {base_user}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Failure classification — structured, LLM-actionable
# ---------------------------------------------------------------------------

_APT_MISS = re.compile(r"Unable to locate package (\S+)")
_PIP_MISS = re.compile(r"No matching distribution found for (\S+)")
#: ``(from <dist>)`` is optional but decisive: pip prints it when the requirement was
#: pulled in by another distribution rather than asked for directly. Dropping it is what
#: made every missing dependency look like a mistake in ``build.python_packages``.
#: The ``(?!versions:)`` matters: pip puts two parenthesised clauses on this line --
#: ``requirement roqsim (from roqsim-mobile-logistics) (from versions: none)`` -- and
#: without it the "no candidates" clause would be read as the requiring package.
_PIP_NAME = re.compile(
    r"Could not find a version that satisfies the requirement (\S+)"
    r"(?: \(from (?!versions:)([^)\s]+)\))?")


def _canonical_name(spec: str) -> str:
    """The PEP 503 name of a requirement spec, for comparing declared against missing.

    Entries are authored as ``roqsim_sensors``, ``roqsim-sensors>=1.2``, ``pkg[extra]``
    or a local path; pip reports the canonical form. Comparing raw strings would call a
    declared package undeclared over a hyphen.
    """
    name = re.split(r"[<>=!~\[;]", spec.strip(), maxsplit=1)[0]
    name = name.strip().rstrip("/")
    # A local path entry (``./``, ``pkgs/foo.whl``) contributes no comparable name.
    if name in ("", ".", "..") or "/" in name or name.endswith(".whl"):
        return ""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declares(spec: Optional[BuildSpec], requirement: str) -> bool:
    """Whether *requirement* is something this build was actually asked to install."""
    if spec is None:
        return False
    wanted = _canonical_name(requirement)
    return bool(wanted) and wanted in {
        _canonical_name(entry) for entry in spec.python_specs}


def classify_build_error(log: str, spec: Optional[BuildSpec] = None) -> ImageBuildError:
    """Map raw builder output to a structured, actionable error.

    Distinguishes agent-fixable failures (apt/pip/source-build → points at the
    ``build:`` entry) from infra failures (base pull / registry push), so the agent
    knows whether editing the ``.vast`` can help.

    *spec* is what makes the pip advice trustworthy. Without it a missing distribution
    can only be reported as such; with it, the classifier can tell a package the build
    asked for (fix the list) from a dependency of one it installed (the base image is
    missing it, and adding the name to the list would paper over that). Pass it wherever
    it is available.
    """
    tail = "\n".join(log.splitlines()[-40:])

    m = _APT_MISS.search(log)
    if m:
        return ImageBuildError(
            phase="apt", fixable_by="agent", entry=m.group(1),
            message=f"apt could not locate package '{m.group(1)}' "
                    "(check build.system_packages)",
            log_tail=tail)

    if "no such option: --python" in log:
        # The install targets the venv from the base's own pip, which has needed
        # ``--python`` since 22.3. Only a hand-picked base_image can be older, and no
        # amount of editing the package list fixes it — say which knob does.
        return ImageBuildError(
            phase="pip", fixable_by="agent", entry="base_image",
            message="the base image's pip is too old to install into the experiment "
                    "venv (needs pip >= 22.3); pick a newer build.base_image",
            log_tail=tail)

    named = _PIP_NAME.search(log)
    m = named or _PIP_MISS.search(log)
    if m:
        missing = m.group(1)
        required_by = named.group(2) if named and named.lastindex and named.lastindex > 1 else ""
        if _declares(spec, missing):
            # Asked for by name and not found: the entry itself is wrong (typo, wrong
            # index, no such version). Pointing at the package list is right.
            return ImageBuildError(
                phase="pip", fixable_by="agent", entry=missing,
                message=f"pip found no matching distribution for '{missing}' "
                        "(check build.python_packages)",
                log_tail=tail)
        if required_by:
            # Nobody asked for it -- it is a dependency of something that was installed,
            # so it was expected to come from the image already. Editing the package list
            # cannot fix that, and adding the name there would only paper over a base
            # image that is missing what this project builds on.
            where = f" (base image: {spec.base_image})" if spec and spec.base_image else ""
            # Only claim it is undeclared when a spec was actually checked; without one
            # that would be the same unfounded certainty this branch exists to remove.
            undeclared = (", and is not declared in build.python_packages"
                          if spec is not None else "")
            return ImageBuildError(
                phase="base-image", fixable_by="agent", entry=missing,
                message=f"'{missing}' is required by '{required_by}' but is not in the "
                        f"image this container builds on{where}{undeclared}. Re-pin "
                        "execution.containers.<name>.image to one that carries it",
                log_tail=tail)
        # No spec to check against, or pip named no requiring distribution: say what
        # happened without asserting where the fix is.
        return ImageBuildError(
            phase="pip", fixable_by="agent", entry=missing,
            message=f"pip found no matching distribution for '{missing}'",
            log_tail=tail)

    low = log.lower()
    if ("pull access denied" in low or "manifest unknown" in low
            or "not found: manifest" in low or "failed to resolve source" in low):
        return ImageBuildError(
            phase="base-pull", fixable_by="infra",
            message="could not pull the base image (server-side registry/base "
                    "config); not fixable by editing build:",
            log_tail=tail)
    # Push failures: name the actual cause. These are all "infra", but the operator has to
    # know *which* knob — this branch used to assert "registry credentials" for every push
    # failure, which sends you looking for a Secret when the registry host simply does not
    # resolve from inside the cluster.
    if "failed to push" in low or "error pushing" in low or "denied" in low or "unauthorized" in low:
        if "no such host" in low or "server misbehaving" in low:
            detail = ("the registry hostname does not resolve from inside the cluster "
                      "(DNS); the build itself succeeded")
        elif ("x509" in low or "certificate signed by unknown authority" in low
                or "tls: failed to verify" in low):
            detail = ("the registry's TLS certificate is not trusted by the build Job "
                      "(set ROBOVAST_REGISTRY_CA_FILE, or INSECURE for a throwaway "
                      "registry); the build itself succeeded")
        elif ("unauthorized" in low or "denied: requested access" in low
                or "authentication required" in low):
            detail = ("the registry rejected the credentials (server-side push Secret); "
                      "the build itself succeeded")
        else:
            detail = "the build itself succeeded; see the log tail for the push error"
        return ImageBuildError(
            phase="push", fixable_by="infra",
            message=f"could not push to the registry: {detail}. "
                    "Not fixable by editing build:",
            log_tail=tail)
    if "no space left on device" in low or "killed" in low or "oomkilled" in low:
        return ImageBuildError(
            phase="resource", fixable_by="infra",
            message="the builder ran out of resources (disk/memory)",
            log_tail=tail)
    if "pip install" in low or "error: subprocess-exited-with-error" in low:
        return ImageBuildError(
            phase="source-build", fixable_by="agent",
            message="a pip install / source build step failed; see the log tail",
            log_tail=tail)
    return ImageBuildError(
        phase="build", fixable_by="agent",
        message="the image build failed; see the log tail", log_tail=tail)


def validate_build_spec(spec: BuildSpec, project_dir: Path) -> list:
    """Fail-fast checks on a ``build:`` section (before any build runs).

    Returns a list of human-readable problems tagged to the ``build.*`` field —
    the analog of config-filter-rejected-at-submit. Empty list ⇒ ok.
    """
    problems = []
    for pkg in spec.system_packages:
        if "/" in pkg or " " in pkg.strip():
            problems.append(
                f"build.system_packages: '{pkg}' does not look like an apt package name")
    for entry in spec.python_specs:
        if _is_source_dir(entry, project_dir) or _is_context_wheel(entry, project_dir):
            continue
        is_pip_url = ("git+" in entry or "://" in entry or " @ " in entry)
        looks_like_path = (
            entry.startswith((".", "/")) or entry.endswith(".whl")
            or ("/" in entry and not is_pip_url))
        if looks_like_path:
            problems.append(
                f"build.python_packages: '{entry}' looks like a workspace path but "
                "no such directory/wheel exists in the project")
        # otherwise treat as a pip spec (index pin / git URL) — not resolvable offline
    return problems


# ---------------------------------------------------------------------------
# Local build manager — docker buildx build --load
# ---------------------------------------------------------------------------

@dataclass
class _BuildRecord:
    build_id: str
    tag: str
    image_hash: str
    local_ref: str
    log_path: Path
    status: ImageBuildStatus
    thread: Optional[threading.Thread] = None


def local_image_ref(tag: str, image_hash: str) -> str:
    """Deterministic local docker tag for an agent-built experiment image.

    ``sim-suite-mobile`` (+ hash) → ``robovast-build/sim-suite-mobile:<hash>``. The
    ``:version`` part of a ``name:version`` tag is folded into the repo name so the
    hash stays the docker tag (one image identity per input set).
    """
    name = tag.replace(":", "-")
    return f"{_LOCAL_TAG_NS}/{name}:{image_hash}"


class LocalImageBuildManager:
    """Runs experiment-image builds on the local Docker daemon (buildx --load).

    Idempotent: a build whose inputs hash to an image already present in the local
    daemon is a no-op cache hit. Thread-per-build with a log file, mirroring the
    campaign worker pattern.
    """

    def __init__(self, log_root: Path):
        self._log_root = Path(log_root)
        self._log_root.mkdir(parents=True, exist_ok=True)
        self._builds: dict[str, _BuildRecord] = {}
        self._by_ref: dict[str, str] = {}   # local_ref -> build_id (most recent)
        self._lock = threading.Lock()

    # -- resolution ---------------------------------------------------------

    def resolve_ref(self, spec: BuildSpec, project_dir: Path) -> str:
        """Concrete local ref for a project's ``build:`` image (no build run)."""
        base_ref = self._base_ref(spec)
        return local_image_ref(spec.tag, build_hash(spec, project_dir, base_ref))

    def image_exists(self, local_ref: str) -> bool:
        try:
            r = subprocess.run(["docker", "image", "inspect", local_ref],
                               capture_output=True, timeout=30, check=False)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _base_ref(self, spec: BuildSpec) -> str:
        # Local dev: an explicit base is used verbatim (may be an alias the operator
        # has locally); otherwise the robovast default. Registry-alias resolution is
        # a cluster-config concern handled server-side on the cluster path.
        return spec.base_image or resolve_build_base_image()

    # -- lifecycle ----------------------------------------------------------

    def start(self, spec: BuildSpec, project_dir: Path) -> ImageBuildRef:
        base_ref = self._base_ref(spec)
        image_hash = build_hash(spec, project_dir, base_ref)
        local_ref = local_image_ref(spec.tag, image_hash)
        build_id = f"build-{spec.tag.replace(':', '-')}-{image_hash}"

        # Idempotent cache hit: image already built for these exact inputs.
        if self.image_exists(local_ref):
            status = ImageBuildStatus(
                build_id=build_id, tag=spec.tag, phase="cached", done=True,
                cached=True, image_ref=f"{BUILD_IMAGE_PREFIX}{spec.tag}",
                digest=image_hash)
            with self._lock:
                self._builds[build_id] = _BuildRecord(
                    build_id, spec.tag, image_hash, local_ref,
                    self._log_root / f"{build_id}.log", status)
                self._by_ref[local_ref] = build_id
            return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=True)

        log_path = self._log_root / f"{build_id}.log"
        status = ImageBuildStatus(
            build_id=build_id, tag=spec.tag, phase="building",
            image_ref=f"{BUILD_IMAGE_PREFIX}{spec.tag}", digest=image_hash,
            started_at=_now())
        record = _BuildRecord(build_id, spec.tag, image_hash, local_ref, log_path,
                              status)
        with self._lock:
            self._builds[build_id] = record
            self._by_ref[local_ref] = build_id

        thread = threading.Thread(
            target=self._run, name=f"robovast-{build_id}",
            args=(record, spec, project_dir, base_ref), daemon=True)
        record.thread = thread
        thread.start()
        return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=False)

    def _run(self, record: _BuildRecord, spec: BuildSpec, project_dir: Path,
             base_ref: str) -> None:
        dockerfile = generate_dockerfile(spec, project_dir, base_ref)
        df_path = self._log_root / f"{record.build_id}.Dockerfile"
        df_path.write_text(dockerfile)
        # BuildKit reads ``<dockerfile>.dockerignore`` beside an out-of-context -f
        # Dockerfile, which is the only place to put one here: writing a .dockerignore
        # into the project dir would mutate the user's workspace.
        df_path.with_name(df_path.name + ".dockerignore").write_text(
            render_dockerignore())
        cmd = ["docker", "buildx", "build", "--builder", _LOCAL_BUILDER, "--load",
               "-f", str(df_path), "-t", record.local_ref, str(project_dir)]
        logger.info("Building image %s: %s", record.local_ref, " ".join(cmd))
        try:
            with open(record.log_path, "wb") as log:
                log.write((dockerfile + "\n---\n").encode())
                log.flush()
                proc = subprocess.Popen(
                    cmd, stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
                rc = proc.wait()
        except OSError as e:
            record.status.error = ImageBuildError(
                phase="build", fixable_by="infra",
                message=f"could not launch docker buildx: {e}")
            record.status.phase = "failed"
            record.status.done = True
            record.status.finished_at = _now()
            return

        log_text = _read_text(record.log_path)
        if rc == 0:
            record.status.phase = "succeeded"
        else:
            record.status.phase = "failed"
            record.status.error = classify_build_error(log_text, spec)
        record.status.done = True
        record.status.finished_at = _now()

    # -- queries ------------------------------------------------------------

    def status(self, build_id: str) -> ImageBuildStatus:
        with self._lock:
            record = self._builds.get(build_id)
        if record is None:
            raise KeyError(f"unknown build '{build_id}'")
        return record.status

    def log(self, build_id: str, offset: int = 0) -> LogChunk:
        with self._lock:
            record = self._builds.get(build_id)
        if record is None:
            raise KeyError(f"unknown build '{build_id}'")
        if not record.log_path.exists():
            return LogChunk(text="", next_offset=offset, eof=record.status.done)
        data = record.log_path.read_bytes()
        chunk = data[offset:]
        return LogChunk(text=chunk.decode(errors="replace"),
                        next_offset=len(data), eof=record.status.done)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def project_build_spec(target) -> "Optional[BuildSpec]":
    """Load + validate a config and return its :class:`BuildSpec` map.

    ``target`` is anything carrying a ``config_path`` — a
    :class:`~robovast.service.local_transport.WorkspaceTarget` from the service, or the
    CLI's ``ProjectConfig``. Empty when no container adds packages.
    """
    from robovast.common.common import load_config
    from robovast.common.config import validate_config
    campaign_config = validate_config(load_config(target.config_path))
    return extract_build_specs(campaign_config,
                               Path(target.config_path).parent)
