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
into the structured :class:`~robovast.service.interface.ImageBuildError`. It is the
**recipe**: nothing here knows where an image ends up. That is the *store*
(:mod:`robovast.service.image_store`) -- the local docker daemon or a cluster registry --
which reuses these pure helpers (``build_hash``, ``generate_dockerfile``,
``classify_build_error``) rather than restating them per lane.

Registry invariant: nothing here emits or accepts a registry endpoint, credential,
or registry-qualified ref. The agent-facing image is always the symbolic
``build:<tag>``; concrete refs are formed by the backend (a local docker tag here,
a ``<registry_prefix>/<tag>:<hash>`` on the cluster) and never returned to a client.
"""

import hashlib
import logging
import os
import re
import subprocess  # nosec B404 - git ls-remote on config-declared URLs
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from robovast.common.build_context import BUILD_CONTEXT_IGNORE
from robovast.common.containers import plan_containers
from robovast.common.execution import (BUILD_IMAGE_PREFIX, DEFAULT_IMAGE_USER,
                                       FAMILY_IMAGE_PREFIX)
from robovast.service.interface import ImageBuildError, ImageBuildRef, ImageBuildStatus

logger = logging.getLogger(__name__)

#: Where the project dir is COPYed inside the image build context.
_CONTEXT_DIR = "/robovast_build_context"
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

#: BuildKit secret id for the git token, and where it appears inside the build. The same id
#: ``container/robovast/Dockerfile.roqsim`` and ``build.sh`` already use for their own clone --
#: one convention, so an operator configures a token once.
GIT_TOKEN_SECRET_ID = "git_token"
_GIT_TOKEN_SECRET_PATH = f"/run/secrets/{GIT_TOKEN_SECRET_ID}"

#: What a pip install of a **private** git spec needs, prepended to the install command.
#:
#: Scoped to ``https://github.com`` rather than answering every prompt: an askpass helper hands the
#: token to whatever asks, so the first spec naming a second host would send a GitHub credential to
#: it. Configured through ``GIT_CONFIG_COUNT`` (git >= 2.31), which is environment-only -- no
#: ``.gitconfig`` is written, so nothing about the credential can survive into a layer.
#:
#: Harmless when no secret is mounted: a public clone is never challenged, so the helper is not
#: invoked, and BuildKit leaves the mount point simply absent. Which is why this rides on every
#: install group carrying a VCS spec rather than being decided per repository -- nothing here can
#: tell a private repository from a public one, and guessing would be the failure.
_GIT_CREDENTIAL_ENV = (
    "GIT_CONFIG_COUNT=1 "
    "GIT_CONFIG_KEY_0=credential.https://github.com.helper "
    "GIT_CONFIG_VALUE_0='!f(){ echo username=x-access-token; "
    f'echo "password=$(cat {_GIT_TOKEN_SECRET_PATH})"; ' + "}; f' "
    "GIT_TERMINAL_PROMPT=0 "
)
_PIP_INSTALL_VCS = (
    "RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked "
    f"--mount=type=secret,id={GIT_TOKEN_SECRET_ID} "
    f"{_GIT_CREDENTIAL_ENV}"
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


def extract_build_specs(campaign_config, base_dir=None, image_project=None,
                        image_project_tag=None) -> dict:
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
    # And resolve the ``family:`` refs it contributed, for the same reason config_generation
    # does it immediately after its own apply_backend: a backend names its member
    # symbolically, because which project and tag it comes from is a property of the
    # campaign and does not exist when the backend runs.
    #
    # Doing it there and not here was a real gap, and an asymmetric one: a container whose
    # image comes from the DEFAULT member assignment was resolved on the composition path,
    # so `sut` and `scenario` built correctly while the one container that declared a
    # `backend:` carried `family:robovast-roqsim` into its Dockerfile's FROM. Docker read
    # that as repository `family`, tag `robovast-roqsim`, and the campaign died in BuildKit
    # with a registry `insufficient_scope` -- which reads as a credentials problem three
    # layers away from the cause.
    from robovast.common.execution import \
        resolve_family_images_in_containers  # pylint: disable=import-outside-toplevel
    resolve_family_images_in_containers(execution_dict.get('containers'),
                                        project=image_project, tag=image_project_tag)
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

#: Where a built image records what it actually contains. Baked into the image rather than
#: written beside the campaign, because that is the only form that survives every path: both
#: lanes get it without extra plumbing, it travels with the image if the image is copied or
#: retagged, and a rebuild a year from now can read what the original installed and install
#: exactly that.
BUILD_MANIFEST_DIR = "/etc/robovast/build-manifest"

#: Plain text, one record per line, rather than JSON. Both are produced by a shell `RUN`, where
#: emitting valid JSON means quoting hundreds of package names correctly and a mistake yields a
#: file that parses as something else; `pip freeze` output is also directly re-installable.
_MANIFEST_FILES = ("apt.txt", "pip.txt", "vcs.txt")


#: Splits ``<name> @ <git+url>`` at the requirement separator. Anchored on the ``git+`` scheme
#: rather than on "the first @", because a name is optional and an ssh URL carries its own.
_VCS_SPLIT = re.compile(r"^(?:(?P<name>[^@\s]+)\s*@\s*)?(?P<url>git\+\S+)$")

_IMMUTABLE_REF = re.compile(r"^[0-9a-f]{40}$")


def _split_url_ref(url: str) -> "tuple[str, str | None, str]":
    """``(url, ref, fragment)`` from a ``git+<url>[@<ref>][#<fragment>]``.

    The fragment is pip's, not git's: ``#subdirectory=pkg`` says which directory of the
    repository holds the distribution, and ``#egg=name`` names it. It follows the ref but is no
    part of it, and reading it as one is how a repository holding several packages -- the only
    reason to write ``#subdirectory=`` at all -- broke twice over. ``@main#subdirectory=pkg``
    resolved as the ref ``main#subdirectory=pkg``, which matches nothing, so the spec was
    refused as unresolvable; and ``@<sha>#subdirectory=pkg`` did not match
    :data:`_IMMUTABLE_REF` either, so a spec that was *already pinned* was refused as well. It
    is returned separately rather than left on either side because :func:`pin_vcs_specs` has to
    put it back: a pinned spec that loses ``#subdirectory=`` installs the repository ROOT,
    which for a multi-package repository is not a distribution at all.

    Two further ``@``-shaped traps, and each defeats the obvious rule for the other:

    * an ssh URL carries a userinfo ``@`` *before* the host
      (``git+ssh://git@host/repo@v1.2.3``), so splitting on the **first** ``@`` yields a URL of
      ``git+ssh://git``; while
    * a ref may contain ``/`` (``@feature/x``), so looking for the ``@`` after the **last** ``/``
      finds nothing at all.

    What separates them reliably is the URL's *authority*: userinfo lives inside it, a ref always
    follows it. So find where the authority ends and take the first ``@`` after that. Neither
    wrong answer would have failed loudly -- the resolution would simply never match, and the
    stale-cache behaviour would return silently.
    """
    # Taken off first, so neither the authority scan nor the ref can see it. A fragment may
    # itself contain '@' (`#egg=pkg&subdirectory=a@b` is legal), which would otherwise be read
    # as the ref separator.
    url, hash_, fragment = url.partition("#")
    fragment = f"{hash_}{fragment}"

    scheme_end = url.find("://")
    authority_end = url.find("/", scheme_end + 3) if scheme_end != -1 else url.rfind("/")
    if authority_end == -1:
        authority_end = len(url)
    at = url.find("@", authority_end)
    if at == -1:
        return url, None, fragment
    return url[:at], url[at + 1:] or None, fragment


def _vcs_specs(specs) -> list:
    """``[(spec, name, url, ref)]`` for every git spec among *specs*. ``ref`` may be ``None``."""
    out = []
    for spec in specs:
        match = _VCS_SPLIT.match(str(spec).strip())
        if not match:
            continue
        url, ref, _fragment = _split_url_ref(match.group("url"))
        out.append((spec, (match.group("name") or "").strip(), url[len("git+"):], ref))
    return out


def resolve_floating_vcs_specs(specs, *, git_token: str = "") -> dict:
    """``{spec: sha}`` for every git spec whose ref is not already a commit.

    A ``plugins``/``python_packages`` entry like ``pkg @ git+https://host/repo@main`` is not a
    pin, and the cache key hashes the spec *string* -- so the first build resolved whatever
    ``main`` was that day and every later campaign silently reused that image. Worse than
    "always latest", because the resolution changed only when something unrelated invalidated
    the key (a renderer epoch bump, a new base image), and nothing recorded which commit had
    been baked in.

    Resolving here fixes both halves at once, exactly as ``container/robovast/build.sh`` already
    does for ``ROQSIM_REF``: the sha goes into the cache key, so a moved branch rebuilds because
    the code really is different; and it goes into the record, so the campaign can say what it
    installed.

    ``git ls-remote`` rather than a clone -- one network round trip, no history.

    Raises:
        ValueError: a ref that cannot be resolved. **Never falls back to the branch name**: that
            would quietly restore the stale-cache behaviour this exists to remove, which is the
            same reasoning build.sh states for refusing.
    """
    resolved = {}
    for spec, _name, url, ref in _vcs_specs(specs):
        if ref and _IMMUTABLE_REF.match(ref):
            continue
        wanted = ref or "HEAD"
        sha = _ls_remote(url, wanted, git_token=git_token)
        if not sha:
            raise ValueError(
                f"cannot resolve {wanted!r} in {url} (from {spec!r}).\n"
                f"  Either the ref does not exist, or the repository needs credentials this "
                f"deployment does not have -- a private repo needs a token from "
                f"'vast exec cluster setup'.\n"
                f"  Not falling back to the bare ref on purpose: that would build from whatever "
                f"the branch points at today and record nothing, which is the behaviour this "
                f"resolution exists to remove. Pin the spec to a commit to proceed without "
                f"network access.")
        resolved[spec] = sha
    return resolved


def _ls_remote(url: str, ref: str, *, git_token: str = "") -> str:
    """The commit *ref* names in *url*, or ``""``.

    Tries the ref verbatim first, then as a branch and a tag: ``ls-remote <url> main`` matches
    ``refs/heads/main``, but an ambiguous or partial name can return several lines, and taking
    the first of those is how you silently pin a tag when you meant a branch.
    """
    env = dict(os.environ)
    if git_token:
        # Same mechanism config_plugins uses for a private plugin repo: credentials via askpass
        # rather than embedded in the URL, so they cannot leak into a build log or a cache key.
        from robovast.common.config_plugins import \
            _git_askpass_env  # pylint: disable=import-outside-toplevel
        env.update(_git_askpass_env(git_token, tempfile.gettempdir()))
    for candidate in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}"):
        try:
            result = subprocess.run(["git", "ls-remote", url, candidate],
                                    capture_output=True, text=True, check=False,
                                    timeout=60, env=env)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            continue
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) == 1:
            return lines[0].split()[0]
        # More than one match: refuse rather than guess which of them was meant.
        if len(lines) > 1 and candidate.startswith("refs/"):
            return ""
    return ""


def pin_vcs_specs(specs, resolved: dict) -> list:
    """*specs* with every resolved floating ref replaced by its commit.

    Applied to what the Dockerfile installs, so the build itself is reproducible: without it a
    branch that moves between resolution and ``pip install`` would install something the record
    does not name.
    """
    out = []
    for spec in specs:
        if isinstance(spec, list):
            out.append(pin_vcs_specs(spec, resolved))
            continue
        sha = resolved.get(spec)
        if not sha:
            out.append(spec)
            continue
        match = _VCS_SPLIT.match(str(spec).strip())
        # The fragment is carried across: it says WHICH package of the repository this is, so
        # dropping it while pinning would install the repository root instead -- a different
        # thing, and usually not a distribution at all.
        url, _ref, fragment = _split_url_ref(match.group("url"))
        name = (match.group("name") or "").strip()
        pinned = f"{url}@{sha}{fragment}"
        out.append(f"{name} @ {pinned}" if name else pinned)
    return out


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


def build_hash(spec: BuildSpec, project_dir: Path, base_identity: str,
               resolved_vcs: "dict | None" = None) -> str:
    """A stable short hash of everything that affects the built image.

    Inputs: the base image's *identity*, apt packages, the python_packages specs, the *contents*
    of every referenced source directory / context wheel, and the commit each floating git spec
    resolved to. Changing any of these changes the hash (a rebuild); anything else (run_files,
    scenario) does not.

    ``base_identity`` is deliberately not the base *ref*. A tag names different bytes before and
    after the base is rebuilt -- and the base is where the dated apt archives are pinned -- so
    hashing the ref would let a snapshot refresh change what gets installed while the key stayed
    equal. ``ImageBuildStore._base_identity`` resolves the ref to the local image ID for this,
    falling back to the ref when the daemon cannot answer.

    ``resolved_vcs`` is what makes a moving branch honest. Without it the key hashed the spec
    *string*, so ``pkg @ git+...@main`` was cache-stable: the first build baked in whatever
    ``main`` was that day, every later campaign reused that image, and the resolution changed
    only when something unrelated invalidated the key -- silently, with nothing recording which
    commit was in there. Hashing the resolved sha means the image rebuilds exactly when the code
    behind the ref really changed, which is the same reasoning ``container/robovast/build.sh``
    gives for resolving ``ROQSIM_REF`` before the build.
    """
    h = hashlib.sha256()
    # v5: source directories install NON-editably now. `-e` routed setuptools through
    # `setup.py develop`, which skips `data_files` -- so an ament_python package's
    # ament-index marker never landed and `ros2 launch <pkg>` could not find it. The epoch
    # is what makes this reach existing projects: their specs and contents are unchanged, so
    # without a bump the hash matches and the service serves the image the OLD renderer
    # built, keeping the broken install semantics after an upgrade.
    #
    # v4: the rendered Dockerfile changed again (installs into a venv at /usr/local, one pip pass
    # per install group instead of one per entry). The Dockerfile text is not itself an input --
    # only the epoch makes a robovast upgrade rebuild rather than serve the image the old renderer
    # produced, which would silently keep the old install semantics.
    h.update(b"v5")
    h.update(base_identity.encode())
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
            sha = (resolved_vcs or {}).get(entry)
            if sha:
                # The resolution, not just the request: a branch that moved must rebuild.
                h.update(b"|vcs|")
                h.update(sha.encode())
            if _is_source_dir(entry, project_dir):
                _hash_dir(h, (project_dir / entry).resolve())
            elif _is_context_wheel(entry, project_dir):
                _hash_wheel(h, (project_dir / entry).resolve())
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Dockerfile generation — one pip pass per install group
# ---------------------------------------------------------------------------

def generate_dockerfile(spec: BuildSpec, project_dir: Path, base_ref: str,
                        base_user: str = DEFAULT_IMAGE_USER,
                        resolved_vcs: "dict | None" = None) -> str:
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
    # The hard error :data:`FAMILY_IMAGE_PREFIX` and :data:`BUILD_IMAGE_PREFIX` both promise.
    # Neither is an image name: one names a member of the published set and one names a build
    # output, and both are meant to be resolved long before here. Written into a FROM they
    # become a repository nothing can pull, and the failure surfaces as a registry
    # authorization error inside a BuildKit log -- far from whatever forgot to resolve it.
    for prefix, what in ((FAMILY_IMAGE_PREFIX, "a RoboVAST image family member"),
                         (BUILD_IMAGE_PREFIX, "a build output")):
        if str(base_ref).startswith(prefix):
            raise ValueError(
                f"unresolved image ref {base_ref!r} reached a Dockerfile FROM. "
                f"{prefix!r} marks {what}, not an image: it must be resolved before a build "
                f"spec is built. This is a bug in whatever produced the spec, not in the "
                f"campaign -- a .vast cannot write one.")

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
        # Install the RESOLVED commit, not the branch. The cache key already accounts for the
        # resolution, but the build must too: a branch that moves between resolution and
        # `pip install` would otherwise install something the record does not name, which is
        # the failure this whole resolution exists to prevent.
        group = pin_vcs_specs(group, resolved_vcs or {})
        args = []
        for entry in group:
            if _is_source_dir(entry, project_dir):
                lines.append(f"COPY {entry} {_CONTEXT_DIR}/{entry}")
                # A REGULAR install, not `-e`. Editable was the odd one out here (the
                # wheel and index-pin branches below never were) and it bought nothing:
                # the COPY above already bakes the source into the image, so there is
                # nothing for editability to keep in sync in an immutable layer.
                #
                # What it did buy was a silent failure. An editable install routes
                # setuptools through `setup.py develop`, which does NOT install
                # `data_files` -- so an `ament_python` package's
                # `share/ament_index/resource_index/packages/<pkg>` entry never lands,
                # `ros2 launch <pkg> ...` cannot resolve it, and the campaign fails with
                # "package not found" listing a search path that includes the very prefix
                # it was installed into. Diagnosed on a Kinova/MoveIt campaign whose
                # move_group never started; the same shape affects every ROS package
                # shipped this way. Cost of the fix is a little duplicated space.
                args.append(f"{_CONTEXT_DIR}/{entry}")
            elif _is_context_wheel(entry, project_dir):
                lines.append(f"COPY {entry} {_CONTEXT_DIR}/{entry}")
                args.append(f"{_CONTEXT_DIR}/{entry}")
            else:
                # index pin or git URL — reused verbatim from plugins: vocabulary.
                # Nothing to copy, so it never carries a context layer.
                args.append(f"'{entry}'")
        # A group holding a git spec gets the credential mount; the rest keep the plain form,
        # so an ordinary install layer is byte-identical to what it always was.
        install = _PIP_INSTALL_VCS if _vcs_specs(group) else _PIP_INSTALL
        lines.append(f"{install} {' '.join(args)}")
    lines.extend(_manifest_lines(spec, resolved_vcs or {}))
    lines.append(f"USER {base_user}")
    return "\n".join(lines) + "\n"


def read_image_build_manifest(image: str) -> dict:
    """``{apt: {...}, pip: {...}, vcs: {...}}`` recorded inside *image*, or ``{}``.

    This is the lock a rebuild installs from. The author's ``.vast`` says ``tree`` and
    ``numpy<=1.13``; the manifest says ``tree=2.2.1-1`` and ``numpy==1.12.1``. Re-resolving the
    loose spec a year later gives a different answer, which is precisely the silent substitution
    a re-run must not make.

    Read by starting a container, and only for an image already present locally: `docker run` on
    an absent image *pulls* it, and a caller asking "what is in this image" must not be the thing
    that fetches gigabytes. ``{}`` means "cannot tell" -- an image built before manifests existed
    has none, and that is a different answer from "installed nothing".
    """
    from robovast.common.execution import \
        _image_present_locally  # pylint: disable=import-outside-toplevel

    if not image or not _image_present_locally(image):
        return {}
    out = {}
    for name in _MANIFEST_FILES:
        text = _read_image_file(image, f"{BUILD_MANIFEST_DIR}/{name}")
        if text is None:
            continue
        key = name.removesuffix(".txt")
        out[key] = _parse_manifest(key, text)
    return out


def pin_specs_from_lock(spec: BuildSpec, lock: dict) -> "tuple[list, list]":
    """``(system_packages, python_packages)`` rewritten to the versions *lock* records.

    The half that makes a build manifest a mechanism rather than a note. The author writes intent
    -- ``tree``, ``numpy<=1.13``, a branch -- and re-resolving that a year later gives a different
    answer, which is exactly the silent substitution a re-run must not make. This turns the
    intent back into what actually ran.

    Only specs the lock actually names are rewritten. A spec the lock does not mention is left
    alone rather than dropped: the lock records what the image *contained*, which includes
    transitive dependencies the author never asked for and may omit something installed another
    way, so treating absence as "remove it" would quietly change the build.

    An apt spec that already carries ``=`` and a pip spec that already carries ``==`` are left
    untouched, because the author pinned them deliberately and the lock is not more authoritative
    than an explicit request.
    """
    apt = lock.get("apt") or {}
    pip = lock.get("pip") or {}

    system = []
    for entry in spec.system_packages or []:
        name = str(entry).strip()
        version = apt.get(name)
        system.append(f"{name}={version}" if version and "=" not in name else name)

    def _pin_python(entry):
        if isinstance(entry, list):
            return [_pin_python(item) for item in entry]
        text = str(entry).strip()
        name = _requirement_name_for_lock(text)
        if not name:
            # A URL, a path, a wheel, or something unparseable. Left exactly as written:
            # rewriting a distribution name over it would substitute a PyPI release for a local
            # wheel or a git commit, which is a different build than the author asked for.
            return entry
        version = pip.get(name) or pip.get(_canonical_pip(name, pip))
        return f"{name}=={version}" if version else entry

    python = [_pin_python(entry) for entry in spec.python_packages or []]
    return system, python


def _requirement_name_for_lock(text: str) -> str:
    """The distribution name in *text*, or ``""`` when it must not be rewritten from a lock.

    A **loose constraint is the case this exists for**: ``numpy<=1.13`` is precisely the spec whose
    re-resolution a year later yields something else. An earlier version matched only bare names
    and therefore left every loose spec unpinned -- doing nothing while appearing to work.

    Returns ``""`` for anything a lock has no business rewriting:

    * a direct reference (``pkg @ git+...``, a path, a wheel) -- the author named exact code, and a
      version from the lock would replace it with a release;
    * an already-``==``-pinned spec -- the author pinned it deliberately, and the lock is not more
      authoritative than an explicit request;
    * an extras or environment-marker expression, where a bare ``name==version`` would drop the
      extras or the marker and change what gets installed.
    """
    try:
        from packaging.requirements import \
            Requirement  # pylint: disable=import-outside-toplevel
        requirement = Requirement(text)
    except Exception:  # pylint: disable=broad-except
        return ""
    if requirement.url or requirement.marker or requirement.extras:
        return ""
    if any(spec.operator in ("==", "===") for spec in requirement.specifier):
        return ""
    return requirement.name


def _canonical_pip(name: str, pip: dict) -> str:
    """The key in *pip* matching *name* under PEP 503 normalisation, or ``name``.

    ``pip freeze`` reports a distribution's own spelling, which may differ from the author's in
    case and in ``-``/``_`` -- so a literal lookup misses and the spec silently stays unpinned.
    """
    wanted = re.sub(r"[-_.]+", "-", name).lower()
    for key in pip:
        if re.sub(r"[-_.]+", "-", key).lower() == wanted:
            return key
    return name


def _read_image_file(image: str, path: str) -> "str | None":
    """One file's contents from inside *image*, or ``None`` if it is not there."""
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--pull=never", "--user", "root",
             "--entrypoint", "cat", image, path],
            capture_output=True, text=True, check=False, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _parse_manifest(kind: str, text: str) -> dict:
    """Parse one manifest file. ``apt`` uses ``=``, ``pip`` uses ``==``, ``vcs`` uses ``->``."""
    separator = {"apt": "=", "pip": "==", "vcs": "->"}[kind]
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or separator not in line:
            continue
        left, _, right = line.partition(separator)
        out[left.strip()] = right.strip()
    return out


def _manifest_lines(spec: BuildSpec, resolved_vcs: dict) -> list:
    """Dockerfile lines that record what this image ended up containing.

    The author writes intent -- a bare apt name, ``numpy<=1.13``, a branch -- and must keep
    being able to: pinning in the source would be wrong, since a *fresh* campaign should pick up
    the current patch release. What has to be pinned is the *re-run*, and that needs the
    resolution written down. This is the lockfile half of that split.

    Emitted last, so it observes the finished image rather than an intermediate layer, and as a
    separate layer so it never invalidates the install layers above it.
    """
    lines = [f"RUN mkdir -p {BUILD_MANIFEST_DIR}"]
    # `dpkg-query` over `apt list --installed`: stable machine format, no locale, no header.
    lines.append(
        f"RUN dpkg-query -W -f='${{Package}}=${{Version}}\\n' 2>/dev/null | sort "
        f"> {BUILD_MANIFEST_DIR}/apt.txt")
    # `|| true`: an image whose base has no pip (a slim non-Python base) is not a broken build,
    # and failing here would turn recording a fact into a reason the campaign cannot exist.
    lines.append(
        f"RUN (pip list --format=freeze 2>/dev/null || true) | sort "
        f"> {BUILD_MANIFEST_DIR}/pip.txt")
    if resolved_vcs:
        # Rendered from what the generator already resolved, not observed in the image: pip
        # records a direct URL per distribution, but not which *requested* ref it came from --
        # and "@main resolved to this commit" is the fact a reader needs to judge a re-run.
        body = "\\n".join(f"{requested} -> {sha}"
                            for requested, sha in sorted(resolved_vcs.items()))
        lines.append(f"RUN printf '{body}\\n' > {BUILD_MANIFEST_DIR}/vcs.txt")
    return lines
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
# Answers about a build that no lane should phrase for itself
# ---------------------------------------------------------------------------

#: Phases in which a build is under way rather than finished, either way -- ``blocked``
#: included, since a pod that cannot start yet has not finished either. The one reader
#: below tests it *after* handling ``blocked`` separately, so that membership changes
#: nothing today; it is here because this is the set's definition, and a later reader
#: asking "is this build over?" about a blocked build must not be told yes.
_IN_FLIGHT = ("pending", "validating", "building", "pushing", "blocked")


def not_built_message(container: str, build_id: str,
                      status: "Optional[ImageBuildStatus]") -> "tuple[str, str]":
    """``(message, next_step)`` for "this container's image is not on the store".

    Pure, so it is testable without a service, and one function so both lanes phrase the
    refusal identically.

    The old message said only "call build_experiment_image first", which is a dead end for
    the caller who *did* call it -- the reported bug this replaces. Four states need four
    different actions, and the service already knows which one it is in, so *status* (the
    build's, or ``None`` when no build is known) picks the wording and the next step:

    ``None``
        nothing was ever started for these inputs.
    in flight
        a build is running. The common case for an agent that execs straight after starting
        one, and previously indistinguishable from "you forgot to build" -- so it must say
        *wait*, not *build again*.
    ``blocked``
        a build exists but its pod cannot start, so nothing is being built and nothing about
        the project would change that. Neither "wait" nor "build again" is right.
    ``failed``
        rebuilding unchanged inputs fails identically; the diagnosis is in the status.
    done, image gone
        the build succeeded and the artifact has since been pruned or deleted. Saying
        "not built" here would deny something that demonstrably happened.

    A build id is only offered as pollable in the states where a build actually exists;
    ``get_image_build_status`` raises on an id nothing started, so advertising it otherwise
    would send the caller into an error.
    """
    phase = getattr(status, "phase", "") if status is not None else ""
    tail = (f"A cache hit reported for another container says nothing about this one. "
            f"This never builds implicitly, so a quick check cannot silently become a "
            f"full image build.")
    if status is not None and phase == "blocked":
        # In flight, but not in the way the branch below means: nothing is being built, the
        # builder itself cannot start. "Wait for it" is the wrong advice -- it is what left a
        # caller waiting on a pod that was never going to run -- and so is "build again",
        # which is where this used to fall through to.
        detail = getattr(getattr(status, "error", None), "message", "") or ""
        because = f": {detail}" if detail else ""
        return (f"the image for container '{container}' cannot be built right now{because}. "
                f"This is the cluster, not the project -- rebuilding changes nothing. {tail}",
                f"get_image_build_status('{build_id}') for error_detail; the build fails on "
                f"its own shortly if the cluster does not resolve this")
    if status is not None and phase in _IN_FLIGHT:
        started = getattr(status, "started_at", None)
        since = f", started {started}" if started else ""
        return (f"the image for container '{container}' is still building "
                f"(build {build_id}, phase {phase}{since}) -- wait for it rather than "
                f"starting another build. {tail}",
                f"run in the background: vast image wait {build_id} --interval 5 "
                f"(exit 0 built, 1 failed)")
    if status is not None and phase == "failed":
        detail = getattr(getattr(status, "error", None), "message", "") or ""
        because = f": {detail}" if detail else ""
        return (f"the image for container '{container}' failed to build{because}. "
                f"Rebuilding the same inputs fails the same way -- read the diagnosis "
                f"and change what it names. {tail}",
                f"get_image_build_status('{build_id}') for error_detail, then "
                f"get_image_build_log(build_id='{build_id}', summarize=True)")
    if status is not None and phase in ("succeeded", "cached"):
        return (f"the image for container '{container}' was built (build {build_id}) and "
                f"is no longer on this lane's image store -- pruned locally, or deleted "
                f"from the registry. It has to be built again. {tail}",
                f"build_experiment_image(container='{container}')")
    return (f"the image for container '{container}' is not built, and no build is "
            f"running for these inputs. {tail}",
            f"build_experiment_image(container='{container}')")


def primary_build_ref(refs: dict) -> ImageBuildRef:
    """Fold one :class:`ImageBuildRef` per container into the single handle a request returns.

    Every build is started; the returned handle names one of them. Prefer the container the
    scenario runs in -- it is the one a caller most likely means -- and carry the rest so
    nothing has to be guessed at.

    ``cached`` is the **conjunction**, and ``cached_builds`` carries the per-container
    answer. It used to be whichever value the primary container happened to have, which
    reported a request as a cache hit while a sibling was still building or had already
    failed -- and the caller, told "nothing to wait for", went straight on to a container
    whose image did not exist. One bool cannot answer a question about several images, so
    the per-image answer is the field and the summary is derived from it.
    """
    from robovast.common.config import SCENARIO_CONTAINER
    primary = refs.get(SCENARIO_CONTAINER) or next(iter(refs.values()))
    primary.builds = {name: ref.build_id for name, ref in refs.items()}
    primary.cached_builds = {name: bool(ref.cached) for name, ref in refs.items()}
    primary.cached = all(primary.cached_builds.values())
    return primary


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
