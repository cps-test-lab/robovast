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

"""Deploy the persistent ``robovast-service`` into a cluster (mode 3).

This is the in-cluster counterpart of ``vast serve``: a long-lived Deployment
running the same FastAPI app (:mod:`robovast.service.app`), reached over a
ClusterIP Service via ``kubectl port-forward`` (Ingress later). It generalises
the ephemeral, per-campaign control channel
(:mod:`robovast.execution.control_server`) into a campaign-spanning service that
launches and monitors controller pods on behalf of thin clients.

The manifests are pure dicts so they can be **server-side dry-run validated**
against a real API server without scheduling anything (see
``deploy_service(dry_run=True)``), and applied idempotently with the kubernetes
Python client — the same style as
:func:`robovast.execution.cluster_execution.cluster_setup.apply_controller_rbac`.

Note on image currency (plan 0.7): the Deployment runs
:func:`robovast.common.execution.resolve_controller_image` with a service
command. That image must contain the ``robovast.service`` package; publish a
service image (or layer the current wheel) before a real rollout — override with
``ROBOVAST_CONTROLLER_IMAGE`` to point at a dev image.
"""

import logging

logger = logging.getLogger(__name__)

SERVICE_NAME = "robovast-service"
SERVICE_ACCOUNT = "robovast-service"
SERVICE_PORT = 8800


def _service_rbac_manifests(namespace):
    """ServiceAccount + Role/RoleBinding letting the service launch controllers.

    The service creates and monitors **controller pods** (and their logs) in its
    namespace — the host's role today — so it needs pod create/read/delete plus
    the ``pods/log`` subresource. It does not itself create scenario Jobs (the
    controllers do), so no ``batch`` verbs here.
    """
    role_name = SERVICE_ACCOUNT
    return [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": SERVICE_ACCOUNT, "namespace": namespace},
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": role_name, "namespace": namespace},
            # The service drives campaigns in-process now (there is no controller
            # pod), so it needs everything that pod's ServiceAccount used to hold.
            "rules": [
                # Scenario runs and the rosbag→CSV postprocessing are Jobs the
                # service creates, watches and reaps.
                {"apiGroups": ["batch"], "resources": ["jobs"],
                 "verbs": ["create", "get", "list", "watch", "delete", "deletecollection"]},
                # read_namespaced_job_status hits the jobs/status subresource,
                # which is a distinct RBAC resource from jobs.
                {"apiGroups": ["batch"], "resources": ["jobs/status"],
                 "verbs": ["get", "list", "watch"]},
                # Job pods (+ logs), and the per-campaign auxiliary-container pods
                # the service creates and tears down.
                {"apiGroups": [""], "resources": ["pods", "pods/log"],
                 "verbs": ["create", "get", "list", "watch", "delete", "deletecollection"]},
                # Variations that declare an auxiliary container run their commands
                # in that campaign's aux pod via the pods/exec subresource (see
                # cluster_execution.container_runner.ClusterContainerRunner).
                {"apiGroups": [""], "resources": ["pods/exec"],
                 "verbs": ["create", "get"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": role_name, "namespace": namespace},
            "subjects": [{"kind": "ServiceAccount", "name": SERVICE_ACCOUNT,
                          "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                        "name": role_name},
        },
    ]


def _deployment_manifest(namespace, image, env=None, git_secret=False):
    """The robovast-service Deployment (1 replica, stateless — no PVC).

    Binds ``0.0.0.0`` inside the pod (reachable only via the ClusterIP Service +
    port-forward/Ingress — the pod network is the boundary). Runs ``vast serve``.

    When *git_secret* is set, the GitHub token Secret is mounted **read-only as a
    file** (never exposed as an env var, so it is not inherited by child processes
    or visible to composition code) at :data:`GIT_TOKEN_MOUNT_DIR`.
    """
    container = {
        "name": SERVICE_NAME,
        "image": image,
        "imagePullPolicy": "Always",
        "command": ["vast", "serve",
                    "--host", "0.0.0.0", "--port", str(SERVICE_PORT)],
        "ports": [{"containerPort": SERVICE_PORT, "name": "http"}],
        "env": list(env or []),
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": SERVICE_PORT},
            "initialDelaySeconds": 5, "periodSeconds": 10},
        "livenessProbe": {
            "httpGet": {"path": "/healthz", "port": SERVICE_PORT},
            "initialDelaySeconds": 15, "periodSeconds": 20},
    }
    pod_spec = {
        "serviceAccountName": SERVICE_ACCOUNT,
        "containers": [container],
    }
    if git_secret:
        container["volumeMounts"] = [{
            "name": "git-credentials", "mountPath": GIT_TOKEN_MOUNT_DIR,
            "readOnly": True}]
        pod_spec["volumes"] = [{
            "name": "git-credentials",
            "secret": {"secretName": GIT_SECRET_NAME, "defaultMode": 0o400}}]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": SERVICE_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": SERVICE_NAME}},
            "template": {
                "metadata": {"labels": {"app": SERVICE_NAME}},
                "spec": pod_spec,
            },
        },
    }


def _service_manifest(namespace):
    """ClusterIP Service exposing the Deployment on :data:`SERVICE_PORT`."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": SERVICE_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": SERVICE_NAME},
            "ports": [{"port": SERVICE_PORT, "targetPort": SERVICE_PORT, "name": "http"}],
        },
    }


#: Secret + key holding the GitHub token that lets the service install a
#: private-repo (``git+https``) variation plugin declared in a ``.vast``'s
#: ``plugins:``. Sourced from the host env at setup; never reaches a controller pod.
#: **Mounted read-only as a file** (not an env var) so it is not inherited by any
#: child process/command — the path must match ``config_plugins.GIT_TOKEN_FILE``.
GIT_SECRET_NAME = "robovast-git-credentials"
GIT_SECRET_KEY = "token"
GIT_TOKEN_MOUNT_DIR = "/var/run/secrets/robovast-git"
# Host env vars a GitHub token may come from at setup. Shared with the
# compose-time reader (``config_plugins.GIT_TOKEN_ENVS``) so the cluster and a
# local run accept the *same* names — one source of truth, no drift.
from robovast.common.config_plugins import \
    GIT_TOKEN_ENVS as _GIT_TOKEN_HOST_ENVS  # noqa: E402


def _load_setup_dotenv():
    """Load ``.env`` (project dir, then CWD) so setup picks up secrets kept there.

    Mirrors the ``.env`` convention already used for share / ntfy credentials, so a
    ``ROBOVAST_GIT_TOKEN=…`` line in the project's ``.env`` is honored without
    exporting it in the shell. ``override=False`` keeps a real env var authoritative.
    """
    import os
    try:
        from dotenv import load_dotenv  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    try:
        from robovast.common.cli.project_config import \
            ProjectConfig  # pylint: disable=import-outside-toplevel
        pc = ProjectConfig.load()
        if pc and getattr(pc, "config_path", None):
            load_dotenv(os.path.join(os.path.dirname(pc.config_path), ".env"), override=False)
    except Exception:  # pylint: disable=broad-except
        pass
    load_dotenv(override=False)  # CWD .env fallback


def _git_token_from_host_env():
    """Return a GitHub token from the host environment / ``.env``, or ``None``."""
    import os
    _load_setup_dotenv()
    for var in _GIT_TOKEN_HOST_ENVS:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


def _git_secret_manifest(namespace, token):
    """A Secret holding the GitHub token for private-repo plugin installs."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": GIT_SECRET_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "type": "Opaque",
        "stringData": {GIT_SECRET_KEY: token},
    }


def _cluster_env(namespace, config_name, config_kwargs):
    """Env that tells the in-cluster ClusterService how to reach the object store.

    The service (mode 3) reconstructs the same cluster config the controller
    uses, so ``create_campaign`` can stage inputs and controllers can pull them.
    """
    import json
    env = [{"name": "ROBOVAST_NAMESPACE", "value": namespace}]
    if config_name:
        env.append({"name": "ROBOVAST_CLUSTER_CONFIG_NAME", "value": config_name})
        env.append({"name": "ROBOVAST_CLUSTER_CONFIG_KWARGS",
                    "value": json.dumps(config_kwargs or {})})
    return env


def service_manifests(namespace="default", image=None, env=None,
                      config_name=None, config_kwargs=None, git_token=None):
    """Return all robovast-service manifests (RBAC [+ git Secret] + Deployment + Service)."""
    from robovast.common.execution import resolve_controller_image
    image = image or resolve_controller_image()
    if env is None:
        env = _cluster_env(namespace, config_name, config_kwargs)
    # The service no longer launches controller pods, but it still needs an image
    # that contains robovast: the postprocessing Job mounts the conversion scripts
    # in from it via an initContainer. Default it to the SAME image the service
    # runs, so they are always in step.
    if not any(e["name"] == "ROBOVAST_CONTROLLER_IMAGE" for e in env):
        env = [*env, {"name": "ROBOVAST_CONTROLLER_IMAGE", "value": image}]

    extra = []
    if git_token is None:
        git_token = _git_token_from_host_env()
    have_git_secret = bool(git_token)
    if have_git_secret:
        # Provide the token to the service as a read-only mounted file (never an
        # env var — that would be inherited by every child process/command). The
        # service hands it to git only for the plugin-install subprocess.
        extra.append(_git_secret_manifest(namespace, git_token))

    return [
        *_service_rbac_manifests(namespace),
        *extra,
        _deployment_manifest(namespace, image, env=env, git_secret=have_git_secret),
        _service_manifest(namespace),
    ]


def deploy_service(namespace="default", kube_context=None, image=None, env=None,
                   config_name=None, config_kwargs=None, dry_run=False):
    """Create/update the robovast-service (idempotent). Returns the manifest list.

    ``dry_run=True`` performs a **server-side** dry run (validates against the
    real API server / admission, persists nothing) — useful to check the
    manifests without an image or scheduling.
    """
    from kubernetes import client, config  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import \
        ApiException  # pylint: disable=import-outside-toplevel

    config.load_kube_config(context=kube_context)
    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()
    apps = client.AppsV1Api()
    dr = "All" if dry_run else None

    manifests = service_manifests(
        namespace=namespace, image=image, env=env,
        config_name=config_name, config_kwargs=config_kwargs)
    by_kind = {m["kind"]: m for m in manifests}
    sa = by_kind["ServiceAccount"]
    role = by_kind["Role"]
    binding = by_kind["RoleBinding"]
    deployment = by_kind["Deployment"]
    service = by_kind["Service"]
    secret = by_kind.get("Secret")

    # ServiceAccount
    _create_or_ok(lambda: core.create_namespaced_service_account(namespace, sa, dry_run=dr))
    # Role (replace rules on conflict so verb changes apply)
    _create_or_replace(
        lambda: rbac.create_namespaced_role(namespace, role, dry_run=dr),
        lambda: rbac.patch_namespaced_role(role["metadata"]["name"], namespace,
                                           {"rules": role["rules"]}, dry_run=dr))
    # RoleBinding
    _create_or_ok(lambda: rbac.create_namespaced_role_binding(namespace, binding, dry_run=dr))
    # Git-credentials Secret (present only when a token was provided at setup).
    if secret is not None:
        _create_or_replace(
            lambda: core.create_namespaced_secret(namespace, secret, dry_run=dr),
            lambda: core.replace_namespaced_secret(
                GIT_SECRET_NAME, namespace, secret, dry_run=dr))
    # Deployment (replace spec on conflict → rolling update / --upgrade)
    _create_or_replace(
        lambda: apps.create_namespaced_deployment(namespace, deployment, dry_run=dr),
        lambda: apps.patch_namespaced_deployment(SERVICE_NAME, namespace, deployment, dry_run=dr))
    # Service (tolerate existing; spec is stable)
    _create_or_ok(lambda: core.create_namespaced_service(namespace, service, dry_run=dr))

    logger.info("Deployed robovast-service in namespace %s (dry_run=%s)", namespace, dry_run)
    return manifests


def delete_service(namespace="default", kube_context=None):
    """Remove the robovast-service Deployment + Service + RBAC (best-effort).

    Never touches the object store (the durable data home) — safe for
    ``--upgrade`` teardown-then-redeploy.
    """
    from kubernetes import client, config  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import \
        ApiException  # pylint: disable=import-outside-toplevel

    try:
        config.load_kube_config(context=kube_context)
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning("Failed to load kube config for service cleanup: %s", exc)
        return
    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()
    apps = client.AppsV1Api()
    deletions = [
        ("Service", lambda: core.delete_namespaced_service(SERVICE_NAME, namespace)),
        ("Deployment", lambda: apps.delete_namespaced_deployment(SERVICE_NAME, namespace)),
        ("RoleBinding", lambda: rbac.delete_namespaced_role_binding(SERVICE_ACCOUNT, namespace)),
        ("Role", lambda: rbac.delete_namespaced_role(SERVICE_ACCOUNT, namespace)),
        ("ServiceAccount", lambda: core.delete_namespaced_service_account(SERVICE_ACCOUNT, namespace)),
    ]
    for kind, call in deletions:
        try:
            call()
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete service %s: %s", kind, exc)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to delete service %s: %s", kind, exc)


def _create_or_ok(create):
    """Run *create*, tolerating an already-exists (409)."""
    from kubernetes.client.rest import \
        ApiException  # pylint: disable=import-outside-toplevel
    try:
        create()
    except ApiException as exc:
        if exc.status != 409:
            raise


def _create_or_replace(create, patch):
    """Run *create*; on 409 run *patch* so spec/rule changes take effect."""
    from kubernetes.client.rest import \
        ApiException  # pylint: disable=import-outside-toplevel
    try:
        create()
    except ApiException as exc:
        if exc.status != 409:
            raise
        patch()
