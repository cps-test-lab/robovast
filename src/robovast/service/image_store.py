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

It exists so that the store is *named*. Left unnamed -- a class for the local store while
the cluster's identical responsibilities are spread across methods of ``ClusterService`` --
the local manager is reachable on both lanes and quietly answers wrongly on one, and every
new caller needs a hand-written override to be safe. A caller that does not get one (such
as ``_exec_image``) asks the local docker daemon on the cluster, inside a pod that has
none, and reports every built image as unbuilt. A lane that forgets to implement
:class:`ImageBuildStore` cannot be constructed at all, which is the difference between a
checklist and a convention.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from robovast.common.errors import ImageStoreUnavailable
from robovast.common.execution import BUILD_IMAGE_PREFIX
from robovast.service.image_build import BuildSpec, resolve_floating_vcs_specs

logger = logging.getLogger(__name__)

#: Environment variable carrying the git token into `docker buildx --secret ...,env=`. Named
#: with a double underscore like the one config_plugins uses for its askpass helper: it exists
#: for the length of one subprocess and is nothing a user configures.
_GIT_TOKEN_ENV = "ROBOVAST__GIT_TOKEN"


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
      check" leaves a service with no docker CLI reporting every built image as unbuilt.
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

    def resolve_vcs(self, spec: BuildSpec) -> dict:
        """``{spec: commit}`` for every git spec whose ref is not already a commit.

        Part of *resolution*, not of the build, because the identity of the image depends on it:
        a moving branch has to change the cache key or the first build's resolution is served
        forever. See :func:`resolve_floating_vcs_specs` for why falling back to the bare ref is
        refused rather than tolerated.

        Concrete and defined HERE rather than per lane, because "which commit does this ref
        name?" has nothing to do with where an image is stored -- and because a lane that
        simply never called it lost the whole feature silently. The cluster lane did: it hashed
        and rendered without the resolution, so `@main` stayed cache-stable, the Dockerfile
        installed the branch rather than the commit, and no vcs.txt was written. Nothing failed;
        the record was just empty. Both callers a lane must not forget are the ones that take
        ``resolved_vcs``: :func:`build_hash` and :func:`generate_dockerfile`.

        A resolution failure is reported as an unavailable store rather than raised as a build
        error: the question "which image would this be?" genuinely cannot be answered without
        network access to the ref, and answering it with a stale hash is what this removes.
        """
        from robovast.common.config_plugins import \
            _read_git_token  # pylint: disable=import-outside-toplevel

        try:
            return resolve_floating_vcs_specs(spec.python_specs,
                                              git_token=_read_git_token())
        except ValueError as e:
            raise ImageStoreUnavailable(str(e)) from e


__all__ = ["ImageRef", "ImageBuildStore", "build_identity"]
