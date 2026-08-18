# Copyright (C) 2026 Frederik Pasch
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

"""Where a lane's experiment images live: the image store.

``image_build`` holds the **recipe** -- which containers build, what their content hash
is, what Dockerfile that renders. This module holds the **store**: given a recipe, what
is the image called here, and is it actually here. That is the only part that differs
between a local ``vast serve`` (the docker daemon) and a cluster deployment (a registry),
and it is the part that must be asked rather than assumed.

It exists because it was previously *not* named. The local store was a class
(``LocalImageBuildManager``) while the cluster's identical responsibilities were spread
across nineteen methods of ``ClusterService`` -- so the local manager was reachable on both
lanes and quietly answered wrongly on one, and every new caller needed a hand-written
override to be safe. One caller (``_exec_image``) did not get one: on the cluster it asked
the local docker daemon, inside a pod that has none, and reported every built image as
unbuilt. A lane that forgets to implement :class:`ImageBuildStore` now cannot be
constructed at all, which is the difference between a checklist and a convention.
"""

import logging
import os
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from robovast.common.build_context import render_dockerignore
from robovast.common.errors import ImageStoreUnavailable
from robovast.common.execution import BUILD_IMAGE_PREFIX, resolve_build_base_image
from robovast.service.image_build import (BuildSpec, build_hash, classify_build_error,
                                          generate_dockerfile)
from robovast.service.interface import (ImageBuildError, ImageBuildRef, ImageBuildStatus,
                                        LogChunk)

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# What a store answers with
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImageRef:
    """One image, in every form its callers need, from a single resolution.

    ``ref``, ``identity`` and ``build_id`` are three *formattings* of the same content hash,
    and ``image_hash`` is that hash itself. Carrying the source value next to the formatted
    ones is not duplication — it is what stops a consumer parsing a hash back out of a name
    to recover it.
    """

    #: The concrete reference a container runs FROM -- a local docker tag, or a
    #: registry-qualified ref. Lane-internal: this one must never reach a client (the
    #: zero-registry-knowledge invariant, see ``image_build``'s module docstring).
    ref: str
    #: ``build:<tag>@<hash>`` -- the registry-free form, and the ONLY one that may cross the
    #: API boundary. Changes exactly when the image changes, so it also keys a cache.
    identity: str
    #: This store's id for the build that produces :attr:`ref`, so a caller told "not built"
    #: is told what to poll or wait for in the same breath.
    build_id: str
    #: The content hash the three names above are built from — what a build records as its
    #: ``digest`` for provenance.
    image_hash: str = ""


def build_identity(tag: str, image_hash: str) -> str:
    """The registry-free identity of a built image: ``build:<tag>@<hash>``.

    Shared by every store so one image has one identity whatever lane produced it -- the
    hash differs per lane (a cluster folds its own base image into it), the *shape* must not.
    """
    return f"{BUILD_IMAGE_PREFIX}{tag}@{image_hash}"


class ImageBuildStore(ABC):
    """Where one lane's built experiment images live.

    An ABC and deliberately not a ``Protocol``: a structural check lets a lane that forgets
    a method fail at whichever call site happens to reach it first, at runtime, which is the
    exact failure this abstraction exists to remove. An ABC refuses to construct the store
    at all and names the missing method.

    Two rules every implementation owes its callers:

    * :meth:`present` **raises** when it cannot tell. Returning ``False`` for "I could not
      check" is how a service with no docker CLI came to report every built image as
      unbuilt.
    * :attr:`ImageRef.ref` stays inside the service; :attr:`ImageRef.identity` is the only
      form handed to a client.
    """

    @abstractmethod
    def ref_for(self, spec: BuildSpec, project_dir: Path) -> ImageRef:
        """What *spec*'s image is called on this store. Resolution only -- builds nothing."""

    @abstractmethod
    def present(self, ref: ImageRef) -> bool:
        """Is that image actually on this store?

        Raises:
            ImageStoreUnavailable: the store could not be asked. Never report this as
                absence -- see the class docstring.
        """


# ---------------------------------------------------------------------------
# The local docker daemon
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

    ``sim-suite-mobile`` (+ hash) -> ``robovast-build/sim-suite-mobile:<hash>``. The
    ``:version`` part of a ``name:version`` tag is folded into the repo name so the
    hash stays the docker tag (one image identity per input set).
    """
    name = tag.replace(":", "-")
    return f"{_LOCAL_TAG_NS}/{name}:{image_hash}"


def local_build_id(tag: str, image_hash: str) -> str:
    """The local store's build id. Derived, not remembered.

    One definition because two callers need the same string from different directions:
    :meth:`LocalDockerImageStore.start`, which records the build under it, and a refusal
    that has only a spec in hand and must still name the build to wait for.
    """
    return f"build-{tag.replace(':', '-')}-{image_hash}"


class LocalDockerImageStore(ImageBuildStore):
    """Experiment images on the local Docker daemon (buildx --load).

    Idempotent: a build whose inputs hash to an image already present in the local
    daemon is a no-op cache hit. Thread-per-build with a log file, mirroring the
    campaign worker pattern.

    ``start`` / ``status`` / ``log`` are not on :class:`ImageBuildStore` yet: submitting and
    reporting a build is still lane-specific code on the transport (``ClusterService``
    overrides those verbs), and only resolution has moved behind the seam. Growing the ABC to
    cover them is what lets those overrides be deleted.
    """

    def __init__(self, log_root: Path):
        self._log_root = Path(log_root)
        self._log_root.mkdir(parents=True, exist_ok=True)
        self._builds: dict[str, _BuildRecord] = {}
        self._by_ref: dict[str, str] = {}   # local_ref -> build_id (most recent)
        self._lock = threading.Lock()

    # -- resolution ---------------------------------------------------------

    def ref_for(self, spec: BuildSpec, project_dir: Path) -> ImageRef:
        base_ref = self._base_ref(spec)
        image_hash = build_hash(spec, project_dir, base_ref)
        return ImageRef(ref=local_image_ref(spec.tag, image_hash),
                        identity=build_identity(spec.tag, image_hash),
                        build_id=local_build_id(spec.tag, image_hash),
                        image_hash=image_hash)

    def present(self, ref: ImageRef) -> bool:
        try:
            r = subprocess.run(["docker", "image", "inspect", ref.ref],  # noqa: S603,S607
                               capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            # Not "the image is absent": the question could not be put. The local lane
            # requires a working docker CLI, so say which dependency is missing rather
            # than blaming the artifact -- reporting this as absence sent one
            # investigation looking for an unbuilt image on a service pod that simply
            # has no docker.
            raise ImageStoreUnavailable(
                f"cannot tell whether {ref.identity} is built: the docker CLI is not "
                f"usable here ({e}). The local lane builds and runs through the docker "
                f"daemon, so this is a deployment problem, not something a rebuild "
                f"fixes.") from e
        return r.returncode == 0

    def _base_ref(self, spec: BuildSpec) -> str:
        # Local dev: an explicit base is used verbatim (may be an alias the operator
        # has locally); otherwise the robovast default. Registry-alias resolution is
        # a cluster-config concern handled server-side on the cluster path.
        return spec.base_image or resolve_build_base_image()

    # -- lifecycle ----------------------------------------------------------

    def start(self, spec: BuildSpec, project_dir: Path) -> ImageBuildRef:
        base_ref = self._base_ref(spec)
        ref = self.ref_for(spec, project_dir)
        image_hash, local_ref, build_id = ref.image_hash, ref.ref, ref.build_id

        # Idempotent cache hit: image already built for these exact inputs.
        if self.present(ref):
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
                proc = subprocess.Popen(  # noqa: S603
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


__all__ = ["ImageRef", "ImageBuildStore", "LocalDockerImageStore",
           "build_identity", "local_image_ref", "local_build_id"]
