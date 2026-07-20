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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from robovast.common.execution import (BUILD_IMAGE_PREFIX, build_image_tag,
                                       is_build_image_ref,
                                       resolve_robovast_image)
from robovast.service.interface import (ImageBuildError, ImageBuildRef,
                                        ImageBuildStatus, LogChunk)

logger = logging.getLogger(__name__)

#: Where the project dir is COPYed inside the image build context.
_CONTEXT_DIR = "/robovast_build_context"
#: Local docker tag namespace for agent-built experiment images.
_LOCAL_TAG_NS = "robovast-build"
#: Files/dirs never hashed or copied into the build context.
_IGNORE = {".git", "__pycache__", ".cache", ".preprocessed", "results",
           "_execution", "_transient", ".robovast_plugins", "resolved"}


@dataclass
class BuildSpec:
    """The normalized ``build:`` section for one project."""

    tag: str
    base_image: Optional[str] = None
    system_packages: list = field(default_factory=list)
    python_packages: list = field(default_factory=list)


def extract_build_spec(campaign_config) -> Optional[BuildSpec]:
    """Pull the ``build:`` section off a validated config, or ``None`` if absent."""
    build = getattr(campaign_config, "build", None)
    if build is None:
        return None
    return BuildSpec(
        tag=build.tag,
        base_image=build.base_image,
        system_packages=list(build.system_packages or []),
        python_packages=list(build.python_packages or []),
    )


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


def build_hash(spec: BuildSpec, project_dir: Path, base_ref: str) -> str:
    """A stable short hash of everything that affects the built image.

    Inputs: the resolved base image, apt packages, the python_packages specs, and
    the *contents* of every referenced source directory / context wheel. Changing
    any of these changes the hash (a rebuild); anything else (run_files, scenario)
    does not.
    """
    h = hashlib.sha256()
    h.update(b"v1")
    h.update(base_ref.encode())
    for pkg in sorted(spec.system_packages):
        h.update(b"|apt|")
        h.update(pkg.encode())
    for entry in spec.python_packages:  # order matters (install order) → not sorted
        h.update(b"|py|")
        h.update(entry.encode())
        if _is_source_dir(entry, project_dir):
            _hash_dir(h, (project_dir / entry).resolve())
        elif _is_context_wheel(entry, project_dir):
            try:
                h.update((project_dir / entry).read_bytes())
            except OSError:
                pass
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Dockerfile generation — one step per entry, for clean error attribution
# ---------------------------------------------------------------------------

def generate_dockerfile(spec: BuildSpec, project_dir: Path, base_ref: str) -> str:
    """Render a deterministic Dockerfile from *spec*.

    Each ``build:`` entry becomes its own ``RUN`` so a failing step maps back to
    exactly one entry (see :func:`classify_build_error`). The base image supplies
    the ROS/nav2 scaffolding *and* ``/etc/robovast_compat_version``, so the result
    stays compat-valid — we never rewrite that marker.
    """
    lines = [f"FROM {base_ref}"]
    if spec.system_packages:
        pkgs = " ".join(spec.system_packages)
        lines.append(
            "RUN apt-get update "
            f"&& apt-get install -y --no-install-recommends {pkgs} "
            "&& rm -rf /var/lib/apt/lists/*")
    if spec.python_packages:
        lines.append(f"COPY . {_CONTEXT_DIR}")
        for entry in spec.python_packages:
            if _is_source_dir(entry, project_dir):
                lines.append(f"RUN pip install -e {_CONTEXT_DIR}/{entry}")
            elif _is_context_wheel(entry, project_dir):
                lines.append(f"RUN pip install {_CONTEXT_DIR}/{entry}")
            else:
                # index pin or git URL — reused verbatim from plugins: vocabulary
                lines.append(f"RUN pip install '{entry}'")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Failure classification — structured, LLM-actionable
# ---------------------------------------------------------------------------

_APT_MISS = re.compile(r"Unable to locate package (\S+)")
_PIP_MISS = re.compile(r"No matching distribution found for (\S+)")
_PIP_NAME = re.compile(r"Could not find a version that satisfies the requirement (\S+)")


def classify_build_error(log: str, spec: Optional[BuildSpec] = None) -> ImageBuildError:
    """Map raw builder output to a structured, actionable error.

    Distinguishes agent-fixable failures (apt/pip/source-build → points at the
    ``build:`` entry) from infra failures (base pull / registry push), so the agent
    knows whether editing the ``.vast`` can help.
    """
    tail = "\n".join(log.splitlines()[-40:])

    m = _APT_MISS.search(log)
    if m:
        return ImageBuildError(
            phase="apt", fixable_by="agent", entry=m.group(1),
            message=f"apt could not locate package '{m.group(1)}' "
                    "(check build.system_packages)",
            log_tail=tail)

    m = _PIP_MISS.search(log) or _PIP_NAME.search(log)
    if m:
        return ImageBuildError(
            phase="pip", fixable_by="agent", entry=m.group(1),
            message=f"pip found no matching distribution for '{m.group(1)}' "
                    "(check build.python_packages)",
            log_tail=tail)

    low = log.lower()
    if ("pull access denied" in low or "manifest unknown" in low
            or "not found: manifest" in low or "failed to resolve source" in low):
        return ImageBuildError(
            phase="base-pull", fixable_by="infra",
            message="could not pull the base image (server-side registry/base "
                    "config); not fixable by editing build:",
            log_tail=tail)
    if ("denied: requested access" in low or "unauthorized" in low
            or "error pushing" in low or "failed to push" in low):
        return ImageBuildError(
            phase="push", fixable_by="infra",
            message="could not push to the registry (server-side registry "
                    "credentials); not fixable by editing build:",
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
    for entry in spec.python_packages:
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
        return spec.base_image or resolve_robovast_image()

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
        cmd = ["docker", "buildx", "build", "--load", "-f", str(df_path),
               "-t", record.local_ref, str(project_dir)]
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


def project_build_spec(project) -> "Optional[BuildSpec]":
    """Load + validate a project's config and return its :class:`BuildSpec`.

    ``project`` is a ``ProjectConfig``-like object with ``config_path``.
    Returns ``None`` when the project has no ``build:`` section.
    """
    from robovast.common.common import load_config
    from robovast.common.config import validate_config
    campaign_config = validate_config(load_config(project.config_path))
    return extract_build_spec(campaign_config)
