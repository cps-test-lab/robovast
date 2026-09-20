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

"""In-cluster experiment-image builds (BuildKit Job + a context staged on the data plane).

The service stages a project's build context (the workspace project dir + a generated
Dockerfile) as a scratch tree on its own disk, then launches a Kubernetes Job whose
init container (``robovast-sidecar``) fetches that tree as one tar stream from the
service's data plane (:mod:`robovast.execution.cluster_execution.pod_access`) and whose
main container has the shared rootless **BuildKit** daemon build+push to the
deployment's registry using a pre-provisioned push Secret. The pushed image is
``<registry_prefix>/<name>:<hash>``; only the symbolic ``build:<tag>`` is ever
returned to a client.

The init container stays even though BuildKit can read an HTTP context by itself: the
data plane wants an ``Authorization`` header, and ``buildctl --opt context=<url>`` has no
way to send one.

The staged context is scratch, not results: it is discarded when the build reaches a
terminal phase, and any context whose Job is gone is swept at the next build (see
:func:`staged_context_build_ids`; the discard is the service's ``discard_staged``). The
Job itself is reaped by its own ``ttlSecondsAfterFinished``.

The pure helpers (hash, Dockerfile, error classification) are shared with the local
path in ``robovast.service.image_build``.
"""

import logging
from pathlib import Path

from robovast.common.execution import GIT_TOKEN_SECRET_ID, resolve_sidecar_image

from . import pod_access

logger = logging.getLogger(__name__)

#: Rootless BuildKit — builds + pushes from within Kubernetes, no docker daemon.
#:
#: PINNED, unlike ``REGISTRY_IMAGE`` beside it, and for the opposite reason. The registry is
#: infrastructure a result never depends on, so a major tag is enough there. This image *is*
#: the builder: which version solved a Dockerfile is part of how an image came to be, and a
#: floating tag means two runs a month apart cannot be said to have built the same way.
#:
#: It also stops being safe the moment the daemon keeps state across builds. BuildKit's
#: on-disk snapshotter format is version-coupled and downgrades are not supported, so a tag
#: that moves under a persistent store is a silent, unattended upgrade of a database — and
#: the ``buildkitd.toml`` GC keys it is configured with have themselves changed across
#: releases (``gckeepstorage`` -> ``reservedSpace``/``maxUsedSpace``/``minFreeSpace``), so
#: the config and the binary have to be chosen together.
#:
#: v0.32.2 is what ``:rootless`` resolved to when this was pinned, so pinning changed
#: nothing that day — which is the point at which to do it.
BUILDKIT_IMAGE = "moby/buildkit:v0.32.2-rootless"

#: Where the staged context (incl. the generated Dockerfile) is fetched to in the Job.
_CONTEXT_MOUNT = "/context"
#: Where the push credential (dockerconfigjson) is mounted for BuildKit.
_DOCKER_CONFIG_MOUNT = "/docker"
#: Where the git token Secret is mounted in the build pod, and the BuildKit secret id the
#: rendered Dockerfile mounts it under. The path mirrors the service pod's own mount, so one
#: Secret -- `robovast-git-credentials`, provisioned at `vast cluster setup` -- serves the
#: composer's plugin clone and a build's private pip install alike.
_GIT_TOKEN_MOUNT = "/var/run/secrets/robovast-git"
#: The build image's own CA bundle, and the writable copy we extend with the registry CA
#: (see the ``SSL_CERT_FILE`` note where the build command is assembled).
_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_CA_BUNDLE = "/tmp/robovast-ca-bundle.crt"


def concrete_image_ref(registry_prefix: str, tag: str, image_hash: str) -> str:
    """Registry-qualified ref for an agent-built image.

    ``registry_prefix='ghcr.io/org'``, ``tag='sim-suite-mobile'`` →
    ``ghcr.io/org/sim-suite-mobile:<hash>``. Server-side only — never returned to a
    client (which sees the symbolic ``build:<tag>``). The ``:version`` part of a
    ``name:version`` tag is folded into the repo name so the hash is the image tag.
    """
    name = tag.replace(":", "-")
    prefix = registry_prefix.rstrip("/")
    return f"{prefix}/{name}:{image_hash}"


def build_id_for(tag: str, image_hash: str) -> str:
    """Deterministic, DNS-1123-safe build/Job id (so a rerun is idempotent)."""
    name = tag.replace(":", "-").replace("_", "-").lower()
    return f"imgbuild-{name}-{image_hash}"


def cache_image_ref(registry_prefix: str, tag: str, scope: str) -> str:
    """Registry ref holding the *layer* cache for a build tag.

    Deliberately **not** hash-qualified: the point is for the build of hash B to import
    the layers produced for hash A, so the cache ref must be shared across hashes of the
    same tag. Each BuildKit Job's build pod is fresh, so a cache mount or emptyDir buys
    nothing across builds — a registry-backed cache is the only layer reuse available
    in-cluster, and it works across nodes and service restarts too.

    ``scope`` is what keeps that sharing from reaching *too* far. The tag is the container's
    name, which is ``sut``/``simulation``/``scenario`` for nearly every project there is, so
    keying on it alone put every project in a deployment on the same three tags — each
    exporting ``mode=max``, overwritten in place, evicting the others' layers -- so a campaign
    whose large stable group was built an hour ago finds it gone because an unrelated campaign
    with a container of the same name built in between. Pass
    :func:`~robovast.service.image_build.cache_scope`, which hashes the chain's *shape* and
    so stays equal across exactly the iterations that should reuse each other.

    The registry therefore holds one cache tag per (container, chain) rather than per
    container.
    """
    name = tag.replace(":", "-")
    return f"{registry_prefix.rstrip('/')}/{name}-{scope}:buildcache"


#: The staged slot every build context lives under, on the service's disk.
BUILD_CONTEXT_PREFIX = "image-builds"


def context_slot(build_id: str) -> str:
    """The staged slot holding *build_id*'s context. One definition, because staging,
    the Job's fetch and the cleanup must all address the same slot."""
    return f"{BUILD_CONTEXT_PREFIX}/{build_id}"


def staged_context_build_ids(contexts_dir: Path) -> set:
    """Build ids that currently have a context staged under *contexts_dir*.

    *contexts_dir* is the service's ``staged_dir(BUILD_CONTEXT_PREFIX)``. The listing *is*
    the record of what needs cleaning: a build id is its directory's name, so no side
    table has to be kept in sync with the disk (and a context staged by a service
    instance that has since restarted is still found).
    """
    contexts_dir = Path(contexts_dir)
    if not contexts_dir.is_dir():
        return set()
    return {child.name for child in contexts_dir.iterdir() if child.is_dir()}


#: Staged-context size above which the build reports what it is carrying. Not a limit --
#: a project may legitimately be large -- but past this the transfer is a visible part of
#: every build's wall time, and a context that grew by accident (see
#: :func:`~robovast.common.build_context.is_campaign_output`) looks exactly like a slow
#: build with no reason given.
CONTEXT_WARN_BYTES = 50 * 1024 * 1024


def stage_context(staged_dir: Path, project_dir: Path, dockerfile: str) -> int:
    """Copy the build context (project dir + generated Dockerfile) into *staged_dir*.

    *staged_dir* is the service's ``staged_dir(context_slot(build_id))``: the tree the
    Job's init container fetches from the data plane. The Dockerfile is written at its
    root, where the BuildKit client reads it. A tree already there is replaced, so a
    re-submitted build stages exactly this project and not the union with a previous one.

    Returns the staged size in bytes, so the caller can put it where someone looking at a
    slow build will see it. This is copied once and fetched into the Job once per
    container per build, and a context that has grown by accident is invisible without
    the figure: a slow build looks exactly like a large one.
    """
    import shutil

    project_dir = Path(project_dir)
    staging = Path(staged_dir)
    if staging.exists():
        shutil.rmtree(staging)
    staged_bytes, staged_files = _copy_tree(project_dir, staging)
    (staging / "Dockerfile").write_text(dockerfile)
    if staged_bytes >= CONTEXT_WARN_BYTES:
        logger.warning(
            "Build context for %s is %.0f MB in %d files, transferred on every build. "
            "The largest directories are: %s. Campaign outputs and the names in "
            "BUILD_CONTEXT_IGNORE are already skipped, so what is left is being sent "
            "on purpose or by accident -- if by accident, move it out of the project.",
            project_dir, staged_bytes / 1e6, staged_files,
            _largest_dirs(staging))
    else:
        logger.info("Staged build context: %.1f MB in %d files",
                    staged_bytes / 1e6, staged_files)
    return staged_bytes


def _largest_dirs(root: Path, top: int = 3) -> str:
    """The heaviest top-level entries of *root*, for a context-size warning."""
    sizes = []
    for child in root.iterdir():
        if child.is_dir():
            total = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
        else:
            total = child.stat().st_size
        sizes.append((total, child.name))
    sizes.sort(reverse=True)
    return ", ".join(f"{name} ({size / 1e6:.0f} MB)" for size, name in sizes[:top]) or "(none)"


def _copy_tree(src: Path, dst: Path) -> tuple:
    """Copy *src* into *dst*, skipping the heavy/irrelevant dirs.

    Uses the shared :data:`~robovast.common.build_context.BUILD_CONTEXT_IGNORE` so
    this staging skips exactly what the local build path hashes over — a mismatch
    would break the context hash.

    Returns ``(bytes, files)`` actually staged.
    """
    import shutil

    from robovast.common.build_context import BUILD_CONTEXT_IGNORE, campaign_outputs_in
    dst.mkdir(parents=True, exist_ok=True)
    # Recognised by structure rather than by name, so a campaign directory sitting beside
    # the sources is skipped whatever it is called. Collected once: `is_campaign_output`
    # stats the filesystem, and asking it per file would do so once per staged file.
    campaigns = set(campaign_outputs_in(src))
    staged_bytes = staged_files = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in BUILD_CONTEXT_IGNORE for part in rel.parts):
            continue
        if any(parent in campaigns for parent in (rel, *rel.parents)):
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            staged_bytes += path.stat().st_size
            staged_files += 1
    return staged_bytes, staged_files


def context_fetch_command(build_id: str) -> str:
    """The init container's shell: fetch the staged context into ``/context``.

    The same ``curl | tar`` every pod uses (:func:`pod_access.fetch_command`); the slot is
    the build's, and the token in the container's env reaches that slot and nothing else.
    """
    return pod_access.fetch_command(f"/staged/{context_slot(build_id)}", _CONTEXT_MOUNT)


#: Where the registry CA (for a self-signed / private-CA registry) is mounted.
_CA_MOUNT = "/certs"
#: Rootless BuildKit reads its config from ``$HOME/.config/buildkit`` (HOME=/home/user).


def _registry_host(image_ref: str) -> str:
    """The registry host[:port] from a full image ref (``host/path:tag`` → ``host``)."""
    return image_ref.split("/", 1)[0]


def build_job_manifest(*, build_id: str, image_ref: str, campaign_label: str,
                       token: str, push_secret_name: str,
                       namespace: str, insecure: bool = False,
                       ca_configmap_name: str = "",
                       cache_ref: str = "", host_aliases: list = None,
                       pull_secret_name: str = "", git_secret_name: str = "",
                       daemon_addr: str) -> dict:
    """A Job that fetches the staged context and has the shared daemon build+push *image_ref*.

    An init container (``robovast-sidecar``) fetches the context from the data plane into
    an emptyDir, reaching it with *token* -- scoped to this build's slot
    (:func:`pod_access.staged_scope`) and carried as a plain env value, because the Job
    lives as long as the slot and the slot holds a copy of a project nothing else can be
    reached through. The BuildKit container builds ``Dockerfile`` from it and pushes with
    the mounted push credential. ``push_secret_name`` is a ``kubernetes.io/dockerconfigjson``
    Secret provisioned at ``vast cluster setup`` — the only place registry credentials live.

    ``pull_secret_name`` authenticates the *pod's own* image pulls, the opposite direction from
    the push above: the init container is ``robovast-sidecar``, which on a private-registry
    deployment needs credentials the kubelet does not otherwise have. Without it the Job's pod
    sits in ``ImagePullBackOff``
    ("no basic auth credentials") while the Job stayed ``active`` — so nothing ever failed
    and ``vast image wait`` never returned. Campaign pods have always carried this (see
    ``kubernetes_backend``); one Secret covers both containers of this pod.

    ``git_secret_name`` is the token a **private** ``python_packages`` git spec needs. It
    reaches the build as a BuildKit secret, so it is readable only by the RUN that installs and
    is in no layer and no image history -- and it is the same Secret the service already holds
    for cloning a private plugin repo, rather than a second credential to configure.

    TLS to a private registry: ``ca_configmap_name`` mounts a CA (key ``ca.pem``), points
    BuildKit at it via ``buildkitd.toml`` **and** puts it on ``SSL_CERT_FILE`` — the
    per-registry ``ca`` covers the registry API, the system pool covers the auth/token
    endpoint (see the note at the command assembly; one without the other is not enough). ``insecure`` instead skips TLS verify
    (plain HTTP / untrusted cert), e.g. a throwaway cluster-internal registry. Prefer
    a CA over ``insecure`` for anything real. (Pull-side trust for a self-signed
    registry is node-level — the operator configures containerd — and is out of
    scope of this Job.)

    ``cache_ref`` (see :func:`cache_image_ref`) turns on the registry layer cache:
    ``mode=max`` exports the intermediate layers, not just the final ones, so a later
    build that changed one late ``build:`` entry reuses everything before it. Export
    failures are non-fatal by design — ``ignore-error=true`` keeps a build from failing
    because the cache tag could not be written (e.g. a read-only or full registry), since
    the image itself has already been pushed at that point.
    """
    # An insecure registry has to be flagged on *every* ref, not just the output: the
    # cache refs address the same registry, so omitting it there fails the import/export
    # with a TLS error while the push succeeds. A mounted CA makes the registry properly
    # trusted, so the flag is neither needed nor wanted then.
    reg_insecure = ",registry.insecure=true" if insecure and not ca_configmap_name else ""
    output = f"type=image,name={image_ref},push=true{reg_insecure}"
    # Required rather than defaulted: an empty address renders `buildctl --addr  build`, which
    # fails somewhere inside the client with a message about the address rather than about the
    # caller that forgot it. There is no sensible default -- the daemon's Service name depends
    # on the namespace.
    #
    # A CLIENT of the shared daemon, not a builder. `--local` paths are resolved and streamed
    # from *here*, so the staged context in this pod's own emptyDir is still exactly right --
    # which is what lets the whole Job (init container, status, logs, idempotent name) stay as
    # it was while the build itself moves somewhere that keeps its store between builds.
    buildctl = (
        f"buildctl --addr {daemon_addr} build "
        "--frontend dockerfile.v0 "
        f"--local context={_CONTEXT_MOUNT} "
        f"--local dockerfile={_CONTEXT_MOUNT} "
        f"--output {output}"
    )
    if cache_ref:
        buildctl += (
            f" --import-cache type=registry,ref={cache_ref}{reg_insecure}"
            f" --export-cache type=registry,ref={cache_ref},mode=max,"
            f"ignore-error=true{reg_insecure}"
        )
    volumes = [{'name': 'context', 'emptyDir': {}}]
    build_mounts = [{'name': 'context', 'mountPath': _CONTEXT_MOUNT}]
    # No BUILDKITD_FLAGS: that is read by `buildctl-daemonless.sh` when it spawns a daemon, and
    # this pod spawns nothing. Same reason the Unconfined seccomp/AppArmor profiles are gone
    # from the container below -- they existed for rootlesskit's mount namespace, which only
    # the daemon creates.
    build_env = []
    if git_secret_name:
        # A BuildKit *secret*, not a build arg or an env: it is mounted for the single RUN
        # that installs, never committed to a layer, and absent from the image's history.
        # `--secret` with no such Secret would fail the build, so a deployment without a
        # token simply builds without one -- which is right, since only a private spec
        # needs it and that case already failed earlier, at resolution.
        volumes.append({
            'name': 'git-credentials',
            # 0444, NOT the 0400 the service pod mounts this same Secret with. BuildKit here is
            # rootless and runs as uid 1000 (see securityContext below), while a Secret volume's
            # files are owned by root -- so an owner-only mode is unreadable and the build dies
            # with `failed to solve: open /var/run/secrets/robovast-git/token: permission
            # denied`, which reads like a mount problem rather than a mode. The push Secret
            # beside it works because it leaves the mode at Kubernetes' readable default.
            'secret': {'secretName': git_secret_name, 'defaultMode': 0o444},
        })
        build_mounts.append({
            'name': 'git-credentials', 'mountPath': _GIT_TOKEN_MOUNT, 'readOnly': True})
        buildctl += f" --secret id={GIT_TOKEN_SECRET_ID},src={_GIT_TOKEN_MOUNT}/token"

    if push_secret_name:
        volumes.append({
            'name': 'docker-config',
            'secret': {
                'secretName': push_secret_name,
                'items': [{'key': '.dockerconfigjson', 'path': 'config.json'}],
            },
        })
        build_mounts.append({
            'name': 'docker-config', 'mountPath': _DOCKER_CONFIG_MOUNT, 'readOnly': True})
        build_env.append({'name': 'DOCKER_CONFIG', 'value': _DOCKER_CONFIG_MOUNT})

    command = ['sh', '-c', buildctl]
    if ca_configmap_name:
        volumes.append({'name': 'registry-ca',
                        'configMap': {'name': ca_configmap_name}})
        build_mounts.append({'name': 'registry-ca', 'mountPath': _CA_MOUNT,
                             'readOnly': True})
        # The per-registry ``ca`` lives in the daemon's own buildkitd.toml rather than here,
        # which is where it belongs: the daemon resolves, pulls and pushes, so it is the side
        # making the TLS connection to the registry API.
        #
        # SSL_CERT_FILE stays HERE as well, and the duplication is deliberate. It covers Go's
        # *system* pool, which is what fetches the OAuth token from the realm named in
        # WWW-Authenticate -- and with only the toml in place that fetch once failed with
        # "x509: certificate signed by unknown authority" *after* the image had been built and
        # exported. That evidence was gathered when client and daemon were one process, so it
        # cannot say which of them made the request, and BuildKit has paths for both depending
        # on what the session negotiates. Splitting the process is exactly what would turn that
        # ambiguity into a failure, so both sides carry the bundle; one of them may be
        # redundant and there is no way to tell which from what we know.
        command = ['sh', '-c',
                   f"{{ cat {_SYSTEM_CA_BUNDLE} 2>/dev/null || true; "
                   f"cat {_CA_MOUNT}/ca.pem; }} > {_CA_BUNDLE} && "
                   f"export SSL_CERT_FILE={_CA_BUNDLE} && "
                   f"{buildctl}"]

    return {
        'apiVersion': 'batch/v1',
        'kind': 'Job',
        'metadata': {
            'name': build_id,
            'namespace': namespace,
            'labels': {'jobgroup': 'image-builds', 'campaign-id': campaign_label,
                       'build-id': build_id},
        },
        'spec': {
            'backoffLimit': 0,
            'ttlSecondsAfterFinished': 3600,
            # A hard stop, needed only since the build moved out of this pod. `ttl` starts
            # when a Job is *terminal*, and with backoffLimit 0 a client wedged against an
            # unreachable or hung daemon leaves both counters at zero -- so the Job stays
            # `active` forever and the TTL never fires. The build was self-contained before
            # and could not hang on anything but itself. Generous: a cold cache legitimately
            # takes many minutes.
            'activeDeadlineSeconds': 3600,
            'template': {
                # No AppArmor/seccomp exemption here: those are for rootlesskit's mount
                # namespace, which this pod does not create. They sit on the daemon, which
                # does.
                'metadata': {
                    'labels': {'jobgroup': 'image-builds', 'build-id': build_id},
                },
                'spec': {
                    'restartPolicy': 'Never',
                    # Names the cluster's DNS cannot resolve (see
                    # BaseConfig.get_host_aliases) — without this a push to such a
                    # registry fails at "lookup <host>: no such host" after the whole
                    # image has already been built.
                    **({'hostAliases': host_aliases} if host_aliases else {}),
                    # Covers the whole pod: the private-registry sidecar init container
                    # and the public BuildKit image alike.
                    **({'imagePullSecrets': [{'name': pull_secret_name}]}
                       if pull_secret_name else {}),
                    'volumes': volumes,
                    'initContainers': [{
                        'name': 'context-fetch',
                        'image': resolve_sidecar_image(),
                        'command': ['sh', '-c', context_fetch_command(build_id)],
                        'env': pod_access.staged_pod_env(namespace, token),
                        'volumeMounts': [{'name': 'context',
                                          'mountPath': _CONTEXT_MOUNT}],
                    }],
                    'containers': [{
                        'name': 'buildkit',
                        'image': BUILDKIT_IMAGE,
                        'command': command,
                        'env': build_env,
                        # uid 1000 is the rootless image's own user, and the git
                        # Secret's 0444 mode below is chosen for it. Nothing else is
                        # relaxed: a client needs no privilege the daemon has.
                        'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000},
                        'volumeMounts': build_mounts,
                    }],
                },
            },
        },
    }
