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

"""In-cluster experiment-image builds (BuildKit Job + S3-staged context).

The service stages a project's build context (the workspace project dir + a
generated Dockerfile) to the object store, then launches a rootless **BuildKit**
Kubernetes Job that mirrors the context in via the ``robovast-sidecar`` init
container (the same ``mc mirror`` contract campaign pods use) and builds+pushes to
the deployment's registry using a pre-provisioned push Secret. The pushed image is
``<registry_prefix>/<name>:<hash>``; only the symbolic ``build:<tag>`` is ever
returned to a client.

The staged context is scratch, not results: it is discarded when the build reaches a
terminal phase, and any context whose Job is gone is swept at the next build (see
:func:`discard_context` / :func:`staged_context_build_ids`). The Job itself is reaped
by its own ``ttlSecondsAfterFinished``.

The pure helpers (hash, Dockerfile, error classification) are shared with the local
path in ``robovast.service.image_build``.
"""

import logging
import tempfile
from pathlib import Path

from robovast.common.execution import resolve_sidecar_image

logger = logging.getLogger(__name__)

#: Rootless BuildKit — builds + pushes from within Kubernetes, no docker daemon.
BUILDKIT_IMAGE = "moby/buildkit:rootless"

#: Where the staged context (incl. the generated Dockerfile) is mirrored in the Job.
_CONTEXT_MOUNT = "/context"
#: Where the push credential (dockerconfigjson) is mounted for BuildKit.
_DOCKER_CONFIG_MOUNT = "/docker"
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


def cache_image_ref(registry_prefix: str, tag: str) -> str:
    """Registry ref holding the *layer* cache for a build tag.

    Deliberately **not** hash-qualified: the point is for the build of hash B to import
    the layers produced for hash A, so the cache ref must be shared across hashes of the
    same tag. Each BuildKit Job's build pod is fresh, so a cache mount or emptyDir buys
    nothing across builds — a registry-backed cache is the only layer reuse available
    in-cluster, and it works across nodes and service restarts too.
    """
    name = tag.replace(":", "-")
    return f"{registry_prefix.rstrip('/')}/{name}:buildcache"


#: Key prefix all staged build contexts live under, inside :func:`build_context_bucket`.
BUILD_CONTEXT_PREFIX = "image-builds"


def context_prefix(build_id: str) -> str:
    """Where *build_id*'s context is staged. One definition, because staging,
    the Job's mirror command, and the cleanup must all address the same keys."""
    return f"{BUILD_CONTEXT_PREFIX}/{build_id}"


def staged_context_build_ids(storage_client, bucket: str) -> set:
    """Build ids that currently have a context staged in *bucket*.

    The listing *is* the record of what needs cleaning: a build id is derivable from
    its own keys, so no side table has to be kept in sync with the object store (and
    a context staged by a service instance that has since restarted is still found).
    """
    head = f"{BUILD_CONTEXT_PREFIX}/"
    ids = set()
    for key in storage_client.list_keys(bucket, BUILD_CONTEXT_PREFIX):
        rest = key[len(head):] if key.startswith(head) else ""
        build_id = rest.split("/", 1)[0]
        if build_id:
            ids.add(build_id)
    return ids


def discard_context(storage_client, bucket: str, build_id: str) -> int:
    """Delete *build_id*'s staged context; return the number of objects removed.

    The context is scratch — a copy of the project dir the init container mirrors in
    once — so it is dead the moment the build reaches a terminal phase. Nothing reads
    it afterwards: a rebuild re-stages, the layer cache lives in the registry, and a
    failure is diagnosed from the build log.
    """
    return storage_client.delete_prefix(bucket, context_prefix(build_id))


def stage_context_to_s3(storage_client, bucket: str, prefix: str,
                        project_dir: Path, dockerfile: str) -> None:
    """Upload the build context (project dir + generated Dockerfile) to S3.

    The Dockerfile is written into a temp copy of the tree root so it lands at the
    context root the BuildKit Job reads. Uses the storage client's ``upload_dir``.
    """
    project_dir = Path(project_dir)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "context"
        _copy_tree(project_dir, staging)
        (staging / "Dockerfile").write_text(dockerfile)
        storage_client.upload_dir(str(staging), bucket, prefix)


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy *src* into *dst*, skipping the heavy/irrelevant dirs.

    Uses the shared :data:`~robovast.common.build_context.BUILD_CONTEXT_IGNORE` so
    this staging skips exactly what the local build path hashes over — a mismatch
    would break the context hash.
    """
    import shutil

    from robovast.common.build_context import BUILD_CONTEXT_IGNORE
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in BUILD_CONTEXT_IGNORE for part in rel.parts):
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def context_fetch_command() -> str:
    """The sidecar ``mc mirror`` command that pulls the staged context into /context.

    Mirrors the campaign init contract (see ``kubernetes_backend``): the S3 env is
    provided by :func:`s3_init_env`, and everything under the build prefix lands in
    ``/context`` (including the generated ``Dockerfile``).
    """
    return (
        'mc alias set mystore "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" && '
        f'mc mirror "mystore/$S3_BUCKET/$S3_BUILD_PREFIX/" {_CONTEXT_MOUNT}/'
    )


#: Bucket the build context is staged to when the deployment has no shared bucket.
#: Lowercase and hyphenated because MinIO rejects underscores with an HTTP 400.
BUILD_CONTEXT_BUCKET = "robovast-image-builds"


def build_context_bucket(cluster_config) -> str:
    """The bucket an experiment-image build stages its context to.

    The deployment's shared bucket when it has one (external-S3 / GCS keep everything
    there under key prefixes). Otherwise a dedicated bucket of our own — an image build
    belongs to no campaign, so a per-campaign-bucket deployment has none to hand it.
    That case used to be refused outright, demanding external-S3 mode, which was never a
    real requirement: the embedded MinIO is an ordinary S3 endpoint, the Job takes
    bucket/prefix/endpoint/credentials as plain values, and the S3 client creates a
    missing bucket exactly as it does for a campaign's own bucket.

    Naming our own bucket is only defensible on the ``s3`` backend, where the namespace
    is the deployment's own endpoint and ``_ensure_bucket`` can create it. On GCS a
    bucket name is global to all of Google Cloud — an invented one would collide with a
    stranger's bucket or 403 — and that client does not create buckets at all, so a
    missing shared bucket is a configuration error to report, not a name to guess.
    """
    shared = cluster_config.get_s3_bucket()
    if shared:
        return shared
    backend = cluster_config.get_storage_backend()
    if backend != "s3":
        raise ValueError(
            f"in-cluster image builds on the '{backend}' storage backend need a bucket "
            "configured for this deployment (there is no private namespace to create one "
            "in). Set it at 'vast exec cluster setup' (GCS: -o gcs_bucket=… or "
            "ROBOVAST_GCS_BUCKET).")
    return BUILD_CONTEXT_BUCKET


def s3_init_env(s3_endpoint, s3_access_key, s3_secret_key, bucket, build_prefix,
                prefix_var: str = 'S3_BUILD_PREFIX'):
    """The env an ``mc``-based init container needs to mirror one prefix down.

    *prefix_var* names the variable carrying the prefix. It is a parameter because the
    container-exec lane stages the same way but reads ``S3_EXEC_PREFIX``: sharing the
    connection half while each caller names its own prefix keeps one definition of "how
    an init container reaches the store" without pretending an exec is a build.
    """
    return [
        {'name': 'S3_ENDPOINT', 'value': s3_endpoint},
        {'name': 'S3_BUCKET', 'value': bucket},
        {'name': 'S3_ACCESS_KEY', 'value': s3_access_key},
        {'name': 'S3_SECRET_KEY', 'value': s3_secret_key},
        {'name': prefix_var, 'value': build_prefix},
    ]


#: Where the registry CA (for a self-signed / private-CA registry) is mounted.
_CA_MOUNT = "/certs"
#: Rootless BuildKit reads its config from ``$HOME/.config/buildkit`` (HOME=/home/user).
_BUILDKIT_CONF_DIR = "/home/user/.config/buildkit"


def _registry_host(image_ref: str) -> str:
    """The registry host[:port] from a full image ref (``host/path:tag`` → ``host``)."""
    return image_ref.split("/", 1)[0]


def build_job_manifest(*, build_id: str, image_ref: str, campaign_label: str,
                       init_env: list, push_secret_name: str,
                       namespace: str, insecure: bool = False,
                       ca_configmap_name: str = "",
                       cache_ref: str = "", host_aliases: list = None) -> dict:
    """A rootless BuildKit Job that fetches the S3 context and builds+pushes *image_ref*.

    An init container (``robovast-sidecar``) mirrors the context to an emptyDir; the
    BuildKit container builds ``Dockerfile`` from it and pushes with the mounted
    push credential. ``push_secret_name`` is a ``kubernetes.io/dockerconfigjson``
    Secret provisioned at ``vast exec cluster setup`` — the only place registry
    credentials live.

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
    buildctl = (
        "buildctl-daemonless.sh build "
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
    build_env = [
        {'name': 'BUILDKITD_FLAGS', 'value': '--oci-worker-no-process-sandbox'},
    ]
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
        # Mount the CA and generate a buildkitd.toml pointing the registry at it, so
        # BuildKit trusts the self-signed/private-CA registry (data plane + token
        # endpoint). Heredoc keeps the shell quoting trivial.
        volumes.append({'name': 'registry-ca',
                        'configMap': {'name': ca_configmap_name}})
        build_mounts.append({'name': 'registry-ca', 'mountPath': _CA_MOUNT,
                             'readOnly': True})
        toml = (f'[registry."{_registry_host(image_ref)}"]\n'
                f'  ca=["{_CA_MOUNT}/ca.pem"]\n')
        # Two mechanisms, because they cover different requests:
        #
        # * buildkitd.toml's per-registry ``ca`` — the registry API (blobs, manifests).
        # * SSL_CERT_FILE — Go's *system* cert pool. The registry's OAuth **token
        #   endpoint** (the realm from WWW-Authenticate, e.g. /service/token) is fetched
        #   by the auth transport, which does not consult the per-registry ``ca`` at all:
        #   with only the toml in place the build failed at "failed to fetch oauth token:
        #   … x509: certificate signed by unknown authority", having already built and
        #   exported the image. Appending to the image's bundle in /tmp keeps this working
        #   for rootless BuildKit, which cannot write /etc/ssl/certs.
        command = ['sh', '-c',
                   f"mkdir -p {_BUILDKIT_CONF_DIR} && "
                   f"cat > {_BUILDKIT_CONF_DIR}/buildkitd.toml <<'BKEOF'\n"
                   f"{toml}BKEOF\n"
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
            'template': {
                'metadata': {
                    'labels': {'jobgroup': 'image-builds', 'build-id': build_id},
                    # Rootless BuildKit needs AppArmor unconfined for its
                    # rootlesskit mount namespace, or it dies with
                    # "failed to share mount point: /: permission denied" (seen on
                    # RKE2/containerd). The legacy per-container annotation covers
                    # nodes < 1.30; the modern securityContext.appArmorProfile below
                    # covers >= 1.30.
                    'annotations': {
                        'container.apparmor.security.beta.kubernetes.io/buildkit':
                            'unconfined',
                    },
                },
                'spec': {
                    'restartPolicy': 'Never',
                    # Names the cluster's DNS cannot resolve (see
                    # BaseConfig.get_host_aliases) — without this a push to such a
                    # registry fails at "lookup <host>: no such host" after the whole
                    # image has already been built.
                    **({'hostAliases': host_aliases} if host_aliases else {}),
                    'volumes': volumes,
                    'initContainers': [{
                        'name': 'context-fetch',
                        'image': resolve_sidecar_image(),
                        'command': ['sh', '-c', context_fetch_command()],
                        'env': init_env,
                        'volumeMounts': [{'name': 'context',
                                          'mountPath': _CONTEXT_MOUNT}],
                    }],
                    'containers': [{
                        'name': 'buildkit',
                        'image': BUILDKIT_IMAGE,
                        'command': command,
                        'env': build_env,
                        'securityContext': {
                            'runAsUser': 1000, 'runAsGroup': 1000,
                            'seccompProfile': {'type': 'Unconfined'},
                            'appArmorProfile': {'type': 'Unconfined'},
                        },
                        'volumeMounts': build_mounts,
                    }],
                },
            },
        },
    }
