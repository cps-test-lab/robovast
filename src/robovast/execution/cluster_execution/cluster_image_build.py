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

The pure helpers (hash, Dockerfile, error classification) are shared with the local
path in ``robovast.service.image_build``.
"""

import logging
import tempfile
from pathlib import Path

from robovast.execution.cluster_execution.postprocess_job import SIDECAR_IMAGE

logger = logging.getLogger(__name__)

#: Rootless BuildKit — builds + pushes from within Kubernetes, no docker daemon.
BUILDKIT_IMAGE = "moby/buildkit:rootless"

#: Where the staged context (incl. the generated Dockerfile) is mirrored in the Job.
_CONTEXT_MOUNT = "/context"
#: Where the push credential (dockerconfigjson) is mounted for BuildKit.
_DOCKER_CONFIG_MOUNT = "/docker"


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


def s3_init_env(s3_endpoint, s3_access_key, s3_secret_key, bucket, build_prefix):
    return [
        {'name': 'S3_ENDPOINT', 'value': s3_endpoint},
        {'name': 'S3_BUCKET', 'value': bucket},
        {'name': 'S3_ACCESS_KEY', 'value': s3_access_key},
        {'name': 'S3_SECRET_KEY', 'value': s3_secret_key},
        {'name': 'S3_BUILD_PREFIX', 'value': build_prefix},
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
                       ca_configmap_name: str = "") -> dict:
    """A rootless BuildKit Job that fetches the S3 context and builds+pushes *image_ref*.

    An init container (``robovast-sidecar``) mirrors the context to an emptyDir; the
    BuildKit container builds ``Dockerfile`` from it and pushes with the mounted
    push credential. ``push_secret_name`` is a ``kubernetes.io/dockerconfigjson``
    Secret provisioned at ``vast exec cluster setup`` — the only place registry
    credentials live.

    TLS to a private registry: ``ca_configmap_name`` mounts a CA (key ``ca.pem``) and
    points BuildKit at it via ``buildkitd.toml`` (proper trust — covers both the
    registry API and the auth/token endpoint). ``insecure`` instead skips TLS verify
    (plain HTTP / untrusted cert), e.g. a throwaway cluster-internal registry. Prefer
    a CA over ``insecure`` for anything real. (Pull-side trust for a self-signed
    registry is node-level — the operator configures containerd — and is out of
    scope of this Job.)
    """
    output = f"type=image,name={image_ref},push=true"
    if insecure and not ca_configmap_name:
        output += ",registry.insecure=true"
    buildctl = (
        "buildctl-daemonless.sh build "
        "--frontend dockerfile.v0 "
        f"--local context={_CONTEXT_MOUNT} "
        f"--local dockerfile={_CONTEXT_MOUNT} "
        f"--output {output}"
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
        command = ['sh', '-c',
                   f"mkdir -p {_BUILDKIT_CONF_DIR} && "
                   f"cat > {_BUILDKIT_CONF_DIR}/buildkitd.toml <<'BKEOF'\n"
                   f"{toml}BKEOF\n{buildctl}"]

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
                    'volumes': volumes,
                    'initContainers': [{
                        'name': 'context-fetch',
                        'image': SIDECAR_IMAGE,
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
