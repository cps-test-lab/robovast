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

    Plus a cluster-scoped read-only ClusterRole (nodes + pods) backing the
    ``/usage`` endpoint — see the ClusterRole manifest below.
    """
    role_name = SERVICE_ACCOUNT
    cluster_role_name = f"{SERVICE_ACCOUNT}-usage-{namespace}"
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
                # The registry push Secret and CA ConfigMap: read to authenticate the
                # "is this image already pushed?" probe, to trust a private-CA registry
                # for it, and to detect that they exist at all (see
                # ClusterService._resolve_registry_objects). Read-only, by name.
                {"apiGroups": [""], "resources": ["secrets", "configmaps"],
                 "verbs": ["get"]},
                # Variations that declare an auxiliary container run their commands
                # in that campaign's aux pod via the pods/exec subresource (see
                # cluster_execution.container_runner.ClusterContainerRunner).
                {"apiGroups": [""], "resources": ["pods/exec"],
                 "verbs": ["create", "get"]},
                # Stopping a campaign tears down its Kueue Workloads: list to find
                # the ones owned by the campaign's jobs, delete/deletecollection to
                # remove them, and patch to strip finalizers off any stuck ones
                # (see cluster_execution.kubernetes_kueue.cleanup_kueue_workloads).
                {"apiGroups": ["kueue.x-k8s.io"], "resources": ["workloads"],
                 "verbs": ["get", "list", "watch", "delete", "deletecollection", "patch"]},
                # The admission preflight reads the LocalQueue every job is labelled
                # into, to fail loudly instead of letting Kueue suspend the batch
                # forever (kubernetes_kueue.verify_kueue_admission_ready).
                {"apiGroups": ["kueue.x-k8s.io"], "resources": ["localqueues"],
                 "verbs": ["get", "list"]},
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
        # Cluster-scoped reads: nodes for the /usage endpoint (cluster resources, not
        # grantable via a namespaced Role) with cluster-wide pod requests for the true
        # "used" figure across tenants, and the ClusterQueue behind the LocalQueue for
        # the admission preflight. Read-only (get/list). The ClusterRole name is
        # namespaced so parallel robovast deployments don't collide.
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": cluster_role_name},
            "rules": [
                {"apiGroups": [""], "resources": ["nodes", "pods"],
                 "verbs": ["get", "list"]},
                {"apiGroups": ["kueue.x-k8s.io"], "resources": ["clusterqueues"],
                 "verbs": ["get", "list"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": cluster_role_name},
            "subjects": [{"kind": "ServiceAccount", "name": SERVICE_ACCOUNT,
                          "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                        "name": cluster_role_name},
        },
    ]


def _deployment_manifest(namespace, image, env=None, git_secret=False,
                         env_secret_names=()):
    """The robovast-service Deployment (1 replica, stateless — no PVC).

    Binds ``0.0.0.0`` inside the pod (reachable only via the ClusterIP Service +
    port-forward/Ingress — the pod network is the boundary). Runs ``vast serve``.

    When *git_secret* is set, the GitHub token Secret is mounted **read-only as a
    file** (never exposed as an env var, so it is not inherited by child processes
    or visible to composition code) at :data:`GIT_TOKEN_MOUNT_DIR`.

    *env_secret_names* are the env-secret Secrets (share creds, ntfy creds — see
    :data:`ENV_SECRET_SOURCES`) pulled in via ``envFrom``. Those *must* be env
    vars, because the in-driver upload / notifier read them straight from
    ``os.environ`` (see ``in_pod_upload.load_provider_from_env`` and
    ``Notifier.from_env``).
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
    if env_secret_names:
        container["envFrom"] = [{"secretRef": {"name": n}} for n in env_secret_names]
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


def _git_token_from_host_env():
    """Return a GitHub token from the host environment, or ``None``.

    A ``ROBOVAST_GIT_TOKEN=…`` line in the project ``.env`` is already part of that
    environment: the ``vast`` CLI loads ``./.env`` once before any command runs.
    """
    import os
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


#: Secret holding the resolved share-provider credentials (bucket + inline key
#: JSON/PEM, URL, user, password, …) that the in-cluster service reads from its
#: own environment to stream finished campaigns to the configured share
#: (``--upload-to-share``). Sourced from the host env / ``.env`` at setup and
#: injected into the service pod via ``envFrom`` — the driver's provider reads
#: them straight from ``os.environ`` (see ``in_pod_upload.load_provider_from_env``).
SHARE_SECRET_NAME = "robovast-share-credentials"


def _share_env_from_host():
    """Resolve the configured share provider's pod env from the host, or ``None``.

    Reads ``ROBOVAST_SHARE_TYPE`` (and the provider's own vars) from the host
    environment / project ``.env`` — the same source ``vast serve`` uses locally
    — and asks the provider to materialise its **pod** environment via
    :meth:`~...share_providers.base.BaseShareProvider.build_pod_env`, which
    resolves host credential *files* (a GCS key file, an SFTP key file) into the
    inline values a pod can carry. ``ROBOVAST_SHARE_TYPE`` is included so the
    service picks the same provider back up.

    Returns ``None`` when no share is configured (``ROBOVAST_SHARE_TYPE`` unset).
    Raises :class:`click.UsageError` when a share type is set but unknown, or its
    required credentials are missing/unreadable — fail fast at setup, never
    silently mid-campaign.
    """
    import os
    from .share_providers import \
        load_share_provider_plugins  # pylint: disable=import-outside-toplevel

    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        return None

    providers = load_share_provider_plugins()
    if share_type not in providers:
        import click  # pylint: disable=import-outside-toplevel
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise click.UsageError(
            f"ROBOVAST_SHARE_TYPE='{share_type}' has no registered provider. "
            f"Available: {available}.")

    provider = providers[share_type]()  # constructor validates required env vars
    return {"ROBOVAST_SHARE_TYPE": share_type, **provider.build_pod_env()}


#: Secret holding the ntfy.sh push-notification config (``ROBOVAST_NTFY_TOPIC`` and
#: optional ``ROBOVAST_NTFY_SERVER`` / ``ROBOVAST_NTFY_TOKEN``) the in-service driver's
#: :class:`~robovast.execution.notify.Notifier` reads from its own environment to push
#: per-campaign lifecycle notifications. Sourced from the host env / ``.env`` at setup
#: and injected into the service pod via ``envFrom`` — same shape as the share Secret.
NTFY_SECRET_NAME = "robovast-ntfy-credentials"


def _ntfy_env_from_host():
    """Resolve the ntfy notification env from the host, or ``None`` when disabled.

    Collects whichever of ``ROBOVAST_NTFY_TOPIC`` / ``ROBOVAST_NTFY_SERVER`` /
    ``ROBOVAST_NTFY_TOKEN`` are present in the host environment / project ``.env``
    (the same source ``vast serve`` uses locally). Notifications are **optional**, so
    — unlike :func:`_share_env_from_host` — this never raises: it returns ``None``
    when ``ROBOVAST_NTFY_TOPIC`` is unset, leaving the in-pod ``Notifier`` a no-op.
    """
    import os
    if not os.environ.get("ROBOVAST_NTFY_TOPIC", "").strip():
        return None
    out = {}
    for var in ("ROBOVAST_NTFY_TOPIC", "ROBOVAST_NTFY_SERVER", "ROBOVAST_NTFY_TOKEN"):
        val = os.environ.get(var, "").strip()
        if val:
            out[var] = val
    return out


#: Secret holding the registry *config* the in-cluster service reads from its own
#: env (``envFrom``) to build/ship agent-built experiment images: the registry
#: prefix, the names of the push/pull dockerconfigjson Secrets, and an optional
#: default base image. Read back by ``BaseConfig.get_registry_config()``.
REGISTRY_CONFIG_SECRET_NAME = "robovast-registry-config"

#: dockerconfigjson Secret holding registry push/pull credentials, created only
#: when ``ROBOVAST_REGISTRY_SERVER``/``_USERNAME``/``_PASSWORD`` are set at setup.
#: Mounted into the build Job (``docker push``) and referenced as an
#: ``imagePullSecret`` on campaign pods. An anonymous/insecure registry (e.g. a
#: cluster-internal one) needs no such Secret — the config Secret then names none.
REGISTRY_PUSH_SECRET_NAME = "robovast-registry-push"


def _registry_env_from_host():
    """Resolve the registry config env from the host, or ``None`` when unset.

    Reads ``ROBOVAST_REGISTRY_PREFIX`` (required to enable in-cluster builds) plus
    the optional default base image. When registry auth is also provided (see
    :func:`_registry_dockerconfig_manifest`) the push/pull Secret names are wired so
    the build Job and campaign pods use it.
    """
    import os
    prefix = os.environ.get("ROBOVAST_REGISTRY_PREFIX", "").strip()
    if not prefix:
        return None
    env = {"ROBOVAST_REGISTRY_PREFIX": prefix}
    base = os.environ.get("ROBOVAST_BASE_EXPERIMENT_IMAGE", "").strip()
    if base:
        env["ROBOVAST_BASE_EXPERIMENT_IMAGE"] = base
    if (os.environ.get("ROBOVAST_REGISTRY_USERNAME", "").strip()
            and os.environ.get("ROBOVAST_REGISTRY_PASSWORD", "").strip()):
        env["ROBOVAST_REGISTRY_PUSH_SECRET"] = REGISTRY_PUSH_SECRET_NAME
        env["ROBOVAST_REGISTRY_PULL_SECRET"] = REGISTRY_PUSH_SECRET_NAME
    insecure = os.environ.get("ROBOVAST_REGISTRY_INSECURE", "").strip()
    if insecure:
        env["ROBOVAST_REGISTRY_INSECURE"] = insecure
    # A registry CA file → a ConfigMap the build Job mounts so BuildKit trusts a
    # self-signed / private-CA registry (see _registry_ca_manifest).
    if os.environ.get("ROBOVAST_REGISTRY_CA_FILE", "").strip():
        env["ROBOVAST_REGISTRY_CA_CONFIGMAP"] = REGISTRY_CA_CONFIGMAP_NAME
    # Carried through so a deployed (in-pod) service resolves an unresolvable registry
    # the same way a local one does — the aliases are read from the service's env when
    # it builds a Job spec (BaseConfig.get_host_aliases).
    aliases = os.environ.get("ROBOVAST_EXTRA_HOST_ALIASES", "").strip()
    if aliases:
        env["ROBOVAST_EXTRA_HOST_ALIASES"] = aliases
    return env


#: ConfigMap (key ``ca.pem``) holding the registry CA, created when
#: ``ROBOVAST_REGISTRY_CA_FILE`` is set at setup. Mounted into the build Job.
REGISTRY_CA_CONFIGMAP_NAME = "robovast-registry-ca"


def _registry_ca_manifest(namespace):
    """A ConfigMap holding the registry CA, or ``None`` when no CA file is set."""
    import os
    ca_path = os.environ.get("ROBOVAST_REGISTRY_CA_FILE", "").strip()
    if not ca_path:
        return None
    try:
        ca = open(ca_path, encoding="utf-8").read()
    except OSError as e:
        import click  # pylint: disable=import-outside-toplevel
        raise click.UsageError(
            f"ROBOVAST_REGISTRY_CA_FILE='{ca_path}' is unreadable: {e}") from e
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": REGISTRY_CA_CONFIGMAP_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "data": {"ca.pem": ca},
    }


def _registry_dockerconfig_manifest(namespace):
    """A ``kubernetes.io/dockerconfigjson`` Secret for registry push/pull, or ``None``.

    Created only when ``ROBOVAST_REGISTRY_SERVER`` + ``ROBOVAST_REGISTRY_USERNAME`` +
    ``ROBOVAST_REGISTRY_PASSWORD`` are set at setup (an existing external registry —
    the Phase-1 path). The credentials never leave the cluster and never cross the
    client interface.
    """
    import base64
    import json
    import os
    server = os.environ.get("ROBOVAST_REGISTRY_SERVER", "").strip()
    user = os.environ.get("ROBOVAST_REGISTRY_USERNAME", "").strip()
    password = os.environ.get("ROBOVAST_REGISTRY_PASSWORD", "").strip()
    if not (server and user and password):
        return None
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    dockercfg = {"auths": {server: {"username": user, "password": password,
                                    "auth": auth}}}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": REGISTRY_PUSH_SECRET_NAME, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "type": "kubernetes.io/dockerconfigjson",
        "stringData": {".dockerconfigjson": json.dumps(dockercfg)},
    }


def _env_secret_manifest(namespace, name, env):
    """A Secret holding credentials the service reads from its own env (``envFrom``)."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app": SERVICE_NAME}},
        "type": "Opaque",
        "stringData": dict(env),
    }


#: Env-based credential sources injected into the service pod as Secrets pulled in via
#: ``envFrom`` (see :func:`_deployment_manifest`). Each is ``(secret_name, resolver)``
#: where the resolver reads the host env / ``.env`` and returns the pod env dict, or
#: ``None`` when that credential is not configured. Adding another env-based credential
#: is a one-line registration here — the deploy, redeploy and teardown paths all iterate
#: this list. (The git token is deliberately NOT here: it is a read-only file mount, not
#: env, so it is never inherited by child processes.)
ENV_SECRET_SOURCES = (
    (SHARE_SECRET_NAME, _share_env_from_host),
    (NTFY_SECRET_NAME, _ntfy_env_from_host),
    (REGISTRY_CONFIG_SECRET_NAME, _registry_env_from_host),
)


def _cluster_env(namespace, config_name, config_kwargs, kube_context=None):
    """Env that tells the in-cluster ClusterService how to reach the object store.

    The service (mode 3) reconstructs the same cluster config the controller
    uses, so ``create_campaign`` can stage inputs and controllers can pull them.

    ``kube_context`` records the context this service was deployed with, so the
    in-pod driver can resolve per-cluster resource lists (keyed by context name)
    — in-cluster there is no kubeconfig context to fall back on.
    """
    import json
    env = [{"name": "ROBOVAST_NAMESPACE", "value": namespace}]
    if config_name:
        env.append({"name": "ROBOVAST_CLUSTER_CONFIG_NAME", "value": config_name})
        env.append({"name": "ROBOVAST_CLUSTER_CONFIG_KWARGS",
                    "value": json.dumps(config_kwargs or {})})
    if kube_context:
        env.append({"name": "ROBOVAST_KUBE_CONTEXT", "value": kube_context})
    return env


def service_manifests(namespace="default", image=None, env=None,
                      config_name=None, config_kwargs=None, git_token=None,
                      share_env=None, kube_context=None):
    """Return all robovast-service manifests (RBAC [+ git/share Secrets] + Deployment + Service)."""
    from robovast.common.execution import resolve_controller_image
    image = image or resolve_controller_image()
    if env is None:
        env = _cluster_env(namespace, config_name, config_kwargs, kube_context)
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

    # Env-based credential Secrets (share creds, ntfy config — see ENV_SECRET_SOURCES).
    # Each resolver reads the host env / .env; when configured it becomes a Secret the
    # service pulls in via envFrom. Unlike the git token these *are* env vars, because
    # the in-driver upload / notifier read them from os.environ. ``share_env`` overrides
    # that one source when given (used by tests / callers passing it explicitly).
    env_secret_names = []
    for name, resolver in ENV_SECRET_SOURCES:
        resolved = share_env if (name == SHARE_SECRET_NAME and share_env is not None) \
            else resolver()
        if resolved:
            extra.append(_env_secret_manifest(namespace, name, resolved))
            env_secret_names.append(name)

    # Registry push/pull credentials (dockerconfigjson) — created only when an
    # external registry's auth is configured at setup. Not an envFrom secret: it is
    # mounted into the build Job and referenced as an imagePullSecret by campaign
    # pods (both by name via the registry config env above).
    registry_secret = _registry_dockerconfig_manifest(namespace)
    if registry_secret:
        extra.append(registry_secret)
    registry_ca = _registry_ca_manifest(namespace)
    if registry_ca:
        extra.append(registry_ca)

    return [
        *_service_rbac_manifests(namespace),
        *extra,
        _deployment_manifest(namespace, image, env=env, git_secret=have_git_secret,
                             env_secret_names=env_secret_names),
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
        config_name=config_name, config_kwargs=config_kwargs,
        kube_context=kube_context)
    by_kind = {m["kind"]: m for m in manifests}
    sa = by_kind["ServiceAccount"]
    role = by_kind["Role"]
    binding = by_kind["RoleBinding"]
    cluster_role = by_kind["ClusterRole"]
    cluster_binding = by_kind["ClusterRoleBinding"]
    deployment = by_kind["Deployment"]
    service = by_kind["Service"]
    # There may be several Secrets (git token, share/ntfy/registry creds) and
    # ConfigMaps (registry CA); by_kind collapses same-kind entries, so collect
    # these from the full list.
    secrets = [m for m in manifests if m["kind"] == "Secret"]
    configmaps = [m for m in manifests if m["kind"] == "ConfigMap"]

    # ServiceAccount
    _create_or_ok(lambda: core.create_namespaced_service_account(namespace, sa, dry_run=dr))
    # Role (replace rules on conflict so verb changes apply)
    _create_or_replace(
        lambda: rbac.create_namespaced_role(namespace, role, dry_run=dr),
        lambda: rbac.patch_namespaced_role(role["metadata"]["name"], namespace,
                                           {"rules": role["rules"]}, dry_run=dr))
    # RoleBinding
    _create_or_ok(lambda: rbac.create_namespaced_role_binding(namespace, binding, dry_run=dr))
    # Cluster-scoped read for /usage (replace rules on conflict so grants stay in step)
    _create_or_replace(
        lambda: rbac.create_cluster_role(cluster_role, dry_run=dr),
        lambda: rbac.patch_cluster_role(cluster_role["metadata"]["name"],
                                        {"rules": cluster_role["rules"]}, dry_run=dr))
    _create_or_ok(lambda: rbac.create_cluster_role_binding(cluster_binding, dry_run=dr))
    # Git-token / share-credential Secrets (each present only when configured at
    # setup). Replace on conflict so rotated credentials take effect on re-setup.
    for secret in secrets:
        name = secret["metadata"]["name"]
        _create_or_replace(
            lambda s=secret: core.create_namespaced_secret(namespace, s, dry_run=dr),
            lambda s=secret, n=name: core.replace_namespaced_secret(
                n, namespace, s, dry_run=dr))
    # ConfigMaps (registry CA). Replace on conflict so a rotated CA takes effect.
    for cm in configmaps:
        name = cm["metadata"]["name"]
        _create_or_replace(
            lambda c=cm: core.create_namespaced_config_map(namespace, c, dry_run=dr),
            lambda c=cm, n=name: core.replace_namespaced_config_map(
                n, namespace, c, dry_run=dr))
    # Deployment (patch spec on conflict, so a `setup --force` over a live
    # service updates it in place instead of failing)
    _create_or_replace(
        lambda: apps.create_namespaced_deployment(namespace, deployment, dry_run=dr),
        lambda: apps.patch_namespaced_deployment(SERVICE_NAME, namespace, deployment, dry_run=dr))
    # Service (tolerate existing; spec is stable)
    _create_or_ok(lambda: core.create_namespaced_service(namespace, service, dry_run=dr))

    logger.info("Deployed robovast-service in namespace %s (dry_run=%s)", namespace, dry_run)
    return manifests


def _load_kube_config(kube_context=None):
    """Load the local kubeconfig, turning setup problems into a clean error.

    ``kubernetes.config.load_kube_config`` raises noisy, low-level exceptions
    (``ConfigException``/``FileNotFoundError``) when ``KUBECONFIG`` is unset,
    points at a missing/invalid file, or names a context that isn't present.
    Wrap them in a ``click.ClickException`` so the CLI prints a short, actionable
    message instead of a traceback.
    """
    import click  # pylint: disable=import-outside-toplevel
    from kubernetes import config  # pylint: disable=import-outside-toplevel
    from kubernetes.config.config_exception import \
        ConfigException  # pylint: disable=import-outside-toplevel

    try:
        config.load_kube_config(context=kube_context)
    except (ConfigException, FileNotFoundError) as exc:
        hint = (f" (does context {kube_context!r} exist?)"
                if kube_context else " (is KUBECONFIG set?)")
        raise click.ClickException(
            f"could not load Kubernetes config{hint}: {exc}") from exc


def read_service_config_from_cluster(namespace="default", kube_context=None):
    """Read ``(config_name, config_kwargs)`` from the deployed service's env.

    Setup writes the cluster config the service reconstructs into the Deployment's
    env (:func:`_cluster_env`), so **the cluster is the authoritative source** — no
    local flag file needed. This is what lets ``vast serve --backend cluster -x
    <ctx>`` and the cluster maintenance commands work from any host with kubeconfig
    access, including one that never ran ``setup``. Returns ``(None, {})`` when the
    Deployment (or the config env) is absent.
    """
    import json  # pylint: disable=import-outside-toplevel

    from kubernetes import client  # pylint: disable=import-outside-toplevel
    from kubernetes.client.rest import \
        ApiException  # pylint: disable=import-outside-toplevel

    _load_kube_config(kube_context)
    apps = client.AppsV1Api()
    try:
        dep = apps.read_namespaced_deployment(SERVICE_NAME, namespace)
    except ApiException as e:
        if e.status == 404:
            return None, {}
        raise
    containers = dep.spec.template.spec.containers or []
    env = {e.name: e.value for c in containers for e in (c.env or [])}
    name = env.get("ROBOVAST_CLUSTER_CONFIG_NAME")
    raw = env.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
    return name, (json.loads(raw) if raw else {})


def delete_service(namespace="default", kube_context=None):
    """Remove the robovast-service Deployment + Service + RBAC (best-effort).

    Never touches the object store (the durable data home), so the
    ``cluster cleanup`` → ``cluster setup`` cycle that updates the service
    keeps the campaign data it left behind.
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
    cluster_role_name = f"{SERVICE_ACCOUNT}-usage-{namespace}"
    deletions = [
        ("Service", lambda: core.delete_namespaced_service(SERVICE_NAME, namespace)),
        ("Deployment", lambda: apps.delete_namespaced_deployment(SERVICE_NAME, namespace)),
        *[(f"Secret ({name})",
           lambda n=name: core.delete_namespaced_secret(n, namespace))
          for name, _ in ENV_SECRET_SOURCES],
        ("Secret (git)", lambda: core.delete_namespaced_secret(GIT_SECRET_NAME, namespace)),
        ("ClusterRoleBinding", lambda: rbac.delete_cluster_role_binding(cluster_role_name)),
        ("ClusterRole", lambda: rbac.delete_cluster_role(cluster_role_name)),
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
