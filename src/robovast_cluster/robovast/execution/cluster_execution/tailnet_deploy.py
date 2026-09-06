# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reach the service over a WireGuard tailnet, instead of publishing it.

An Ingress is the other way in, and it asks for things a private deployment often cannot
give: a public DNS record, a certificate for it, and an address that survives the cluster
being torn down and rebuilt. This route asks for none of them. A Tailscale node runs beside
the service, **dials out** to a coordination server, and answers on the tailnet under a
stable name. Nothing listens on the internet, no firewall rule is opened, and the name does
not change when the cluster does.

Coordination is whatever the operator points it at -- Tailscale's own service, or a
self-hosted `Headscale <https://headscale.net>`_. RoboVAST neither knows nor cares which:
it passes ``--login-server`` through and lets the node register.

**Plain HTTP on the tailnet, deliberately.** The transport is already WireGuard, so a
certificate would encrypt what is encrypted. It works because the session cookie's
``Secure`` flag follows the request scheme (``service.app._login_response``), so a browser
keeps the cookie over ``http://`` here -- the reason an Ingress refuses plain HTTP is that
the token would cross an untrusted network, which is exactly what a tailnet is not.

**It does not make builds possible.** The prefix campaigns push to has to be pullable by the
*kubelet*, and nodes are not on the tailnet -- they resolve no tailnet name and hold no key.
So this publishes the service to people, never to the cluster's own container runtime, and
``can_build_images`` stays false. Publishing an Ingress, or pointing at an external
registry, is what changes that.
"""

import logging
import os

logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "robovast-tailnet"

#: Pinned, like every other image this deploys: a floating tag would change what a re-run
#: installs, and this one holds a WireGuard identity.
IMAGE = "tailscale/tailscale:v1.90.6"

#: The credential, and only the credential. Read from the host environment -- a ``.env``
#: line, like the git token and the ntfy topic -- because a pre-auth key passed as an
#: argument lands in shell history, and because a coordination server's address is a
#: deployment fact that must not be written into this repository.
#:
#: *Whether* a cluster is on a tailnet is NOT read from here: that is ``setup --tailnet``.
#: An environment variable applies to whatever context happens to be current, so one
#: ``.env`` and two clusters would publish the one nobody meant to. See
#: :func:`ensure_tailnet`.
LOGIN_SERVER_ENV = "ROBOVAST_TAILNET_LOGIN_SERVER"
AUTHKEY_ENV = "ROBOVAST_TAILNET_AUTHKEY"
HOSTNAME_ENV = "ROBOVAST_TAILNET_HOSTNAME"

#: The name the node claims on the tailnet, and therefore the host users type.
DEFAULT_HOSTNAME = "robovast"

AUTHKEY_SECRET_NAME = "robovast-tailnet-authkey"
AUTHKEY_SECRET_KEY = "authkey"
SERVE_CONFIG_MAP_NAME = "robovast-tailnet-serve"
SERVE_CONFIG_KEY = "serve.json"
SERVE_CONFIG_DIR = "/etc/robovast-tailnet"

#: Tailscale keeps its node identity in this Secret rather than on a volume. A tailnet
#: identity that did not survive a restart would register a *second* node on every roll,
#: leaving the operator's coordination server filling with dead entries and the name users
#: type silently becoming ``robovast-1``.
STATE_SECRET_NAME = "robovast-tailnet-state"


def configured():
    """``(login_server, authkey, hostname)`` from the environment, or ``None``.

    ``None`` when either half of the pair is missing, which is the "not configured" state --
    a login server with no key cannot register, and a key with no server has nothing to
    register with. A half-configured tailnet is a mistake worth refusing rather than
    half-deploying, and :func:`ensure_tailnet` raises on it.
    """
    server = (os.environ.get(LOGIN_SERVER_ENV) or "").strip()
    authkey = (os.environ.get(AUTHKEY_ENV) or "").strip()
    hostname = (os.environ.get(HOSTNAME_ENV) or "").strip() or DEFAULT_HOSTNAME
    if not server and not authkey:
        return None
    if not server or not authkey:
        missing = LOGIN_SERVER_ENV if not server else AUTHKEY_ENV
        raise ValueError(
            f"{missing} is not set, and the other half of the pair is. A tailnet needs both "
            f"a coordination server to register with and a key to register using, so this "
            f"would deploy a node that can never come up. Set it, or unset both.")
    return server, authkey, hostname


def serve_config(service_host, service_port):
    """Tailscale's declarative proxy config: the tailnet's port 80 to the service.

    Port 80 rather than 443 because nothing here holds a certificate; see the module
    docstring for why that is the right trade on a tailnet and the wrong one on an Ingress.

    **A TCP forward rather than an HTTP handler**, and that is not a simplification. The
    HTTP form keys its handlers by ``<host>:<port>`` and relies on the container
    substituting the node's certificate domain into that key. A self-hosted coordination
    server need not issue one -- ours did not -- and the placeholder then survives into the
    config, matches no request, and tailscale falls back to proxying at ``localhost:80``
    where nothing listens. The whole route fails with the node pingable and the config
    reporting itself applied.

    Forwarding the stream needs no domain, no certificate and no host matching, so it
    behaves the same against every coordination server. What it gives up is per-host
    routing, which one service on one node has no use for.
    """
    import json  # noqa: PLC0415

    return json.dumps({
        "TCP": {"80": {"TCPForward": f"{service_host}:{service_port}"}},
    }, indent=2)


def manifests(namespace, login_server, authkey, hostname, service_host, service_port,
              node_selector=None):
    """Everything the tailnet node needs: identity, config, RBAC and the Deployment.

    RBAC is narrow on purpose -- one Secret, by name. The node writes its own WireGuard
    identity there and reads nothing else, so a wider grant would hand a container that
    talks to an outside coordination server more of the cluster than it needs.
    """
    labels = {"app": DEPLOYMENT_NAME}
    return [
        {"apiVersion": "v1", "kind": "Secret",
         "metadata": {"name": AUTHKEY_SECRET_NAME, "namespace": namespace,
                      "labels": labels},
         "stringData": {AUTHKEY_SECRET_KEY: authkey}},
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": SERVE_CONFIG_MAP_NAME, "namespace": namespace,
                      "labels": labels},
         "data": {SERVE_CONFIG_KEY: serve_config(service_host, service_port)}},
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": DEPLOYMENT_NAME, "namespace": namespace,
                      "labels": labels}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {"name": DEPLOYMENT_NAME, "namespace": namespace,
                      "labels": labels},
         "rules": [{"apiGroups": [""], "resources": ["secrets"],
                    "resourceNames": [STATE_SECRET_NAME],
                    "verbs": ["get", "update", "patch"]},
                   # `create` cannot be resourceNamed: the object does not exist yet on the
                   # first run, and a rule naming it would deny the call that makes it.
                   {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": DEPLOYMENT_NAME, "namespace": namespace,
                      "labels": labels},
         "subjects": [{"kind": "ServiceAccount", "name": DEPLOYMENT_NAME,
                       "namespace": namespace}],
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                     "name": DEPLOYMENT_NAME}},
        _deployment(namespace, login_server, hostname, labels, node_selector),
    ]


def _deployment(namespace, login_server, hostname, labels, node_selector):
    """One replica, userspace networking, no privilege at all.

    ``TS_USERSPACE`` is what keeps this deployable on a managed cluster: the alternative
    needs ``NET_ADMIN`` and ``/dev/net/tun`` from the node, which GKE Autopilot refuses
    outright and which would make this the second privileged thing RoboVAST runs. Userspace
    mode costs throughput a web UI does not notice.

    One replica, because two would register two nodes under one name and the tailnet would
    route to whichever won.
    """
    pod_spec = {
        "serviceAccountName": DEPLOYMENT_NAME,
        "containers": [{
            "name": "tailscale",
            "image": IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "env": [
                {"name": "TS_USERSPACE", "value": "true"},
                {"name": "TS_KUBE_SECRET", "value": STATE_SECRET_NAME},
                {"name": "TS_HOSTNAME", "value": hostname},
                {"name": "TS_SERVE_CONFIG",
                 "value": f"{SERVE_CONFIG_DIR}/{SERVE_CONFIG_KEY}"},
                # Both go to `tailscale up`. The login server is what points this at a
                # self-hosted coordination server instead of the public one.
                {"name": "TS_EXTRA_ARGS", "value": f"--login-server={login_server}"},
                {"name": "TS_AUTHKEY", "valueFrom": {"secretKeyRef": {
                    "name": AUTHKEY_SECRET_NAME, "key": AUTHKEY_SECRET_KEY}}},
            ],
            "volumeMounts": [{"name": "serve-config", "mountPath": SERVE_CONFIG_DIR,
                              "readOnly": True}],
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                          "limits": {"cpu": "500m", "memory": "256Mi"}},
        }],
        "volumes": [{"name": "serve-config",
                     "configMap": {"name": SERVE_CONFIG_MAP_NAME}}],
    }
    if node_selector:
        pod_spec["nodeSelector"] = dict(node_selector)
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": DEPLOYMENT_NAME, "namespace": namespace, "labels": labels},
        "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels}, "spec": pod_spec}},
    }


def ensure_tailnet(namespace="default", kube_context=None, node_selector=None,
                   service_host="", service_port=0, enabled=False):
    """Deploy the tailnet node when this cluster asked for one, remove it when it did not.

    **The decision is the flag, never the environment.** Which route publishes a cluster is
    a property of that cluster, and an operator's ``.env`` is not: one file and two contexts
    would otherwise put a node on whichever happened to be current, on a deployment nobody
    meant to publish. So ``vast cluster setup --tailnet`` says *this* cluster is on a
    tailnet, and the environment only supplies the credential -- which belongs there anyway,
    because a pre-auth key on a command line lands in shell history.

    Off unless asked. Reconciled on **every** setup, for the reason the governor DaemonSet
    is: setup writes the cluster's whole configuration, so dropping the flag takes the node
    away rather than leaving one nobody remembers configuring still answering on a tailnet.

    Returns the hostname it published under, or ``""``.
    """
    from kubernetes import client  # noqa: PLC0415

    from .kube_client import load_kube_config  # noqa: PLC0415
    from .kubernetes import apply_manifests  # noqa: PLC0415

    # Nothing dials a cluster above this point. An argument error must not cost a
    # connection -- the same rule setup applies to its storage flags -- and `remove` loads
    # the configuration itself, so the not-asked-for path needs none of it here either.
    if not enabled:
        remove(namespace, kube_context)
        return ""
    settings = configured()
    if settings is None:
        raise ValueError(
            f"--tailnet asks for a tailnet node, but neither {LOGIN_SERVER_ENV} nor "
            f"{AUTHKEY_ENV} is set. The credential is read from the environment (a .env "
            f"line) rather than taken as an argument, so a pre-auth key does not land in "
            f"shell history. Set both, or drop --tailnet.")
    login_server, authkey, hostname = settings
    load_kube_config(context=kube_context)
    apply_manifests(
        client.ApiClient(),
        iter(manifests(namespace, login_server, authkey, hostname,
                       service_host, service_port, node_selector)),
        namespace=namespace)
    # The server is named because the operator has to recognise which tailnet answered; the
    # key never is, and is not logged anywhere else either.
    logger.info("Tailnet node '%s' deployed against %s; the web UI answers at "
                "http://%s once the node registers.", hostname, login_server, hostname)
    return hostname


def remove(namespace="default", kube_context=None):
    """Delete the tailnet node and everything it owns, tolerating absence.

    The state Secret goes with it: it holds a WireGuard identity for a node that will not
    exist, and keeping it would silently rejoin the tailnet if the deployment came back
    under a different configuration.

    Never raises. This runs inside teardown, where failing to remove one object must not
    abandon the rest -- but each failure is reported rather than swallowed.

    Costs **one** call on the ordinary path. Every setup reconciles this, and the ordinary
    deployment has no tailnet at all, so seven unconditional deletes would be seven round
    trips per setup to remove nothing. The Deployment is the object that exists whenever any
    of the others do, so its absence answers for all of them.
    """
    from kubernetes import client  # noqa: PLC0415
    from kubernetes.client.exceptions import ApiException  # noqa: PLC0415

    from .kube_client import load_kube_config  # noqa: PLC0415

    load_kube_config(context=kube_context)
    core, apps = client.CoreV1Api(), client.AppsV1Api()
    try:
        apps.read_namespaced_deployment(name=DEPLOYMENT_NAME, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            return
    except Exception as exc:  # noqa: BLE001 - fall through and try the deletes
        logger.debug("Could not read %s: %s", DEPLOYMENT_NAME, exc)
    rbac = client.RbacAuthorizationV1Api()
    removals = (
        (apps.delete_namespaced_deployment, DEPLOYMENT_NAME),
        (rbac.delete_namespaced_role_binding, DEPLOYMENT_NAME),
        (rbac.delete_namespaced_role, DEPLOYMENT_NAME),
        (core.delete_namespaced_service_account, DEPLOYMENT_NAME),
        (core.delete_namespaced_config_map, SERVE_CONFIG_MAP_NAME),
        (core.delete_namespaced_secret, AUTHKEY_SECRET_NAME),
        (core.delete_namespaced_secret, STATE_SECRET_NAME),
    )
    for delete, name in removals:
        try:
            delete(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Could not remove %s from %s: %s", name, namespace, exc)
        except Exception as exc:  # noqa: BLE001 - a teardown must continue past one object
            logger.warning("Could not remove %s from %s: %s", name, namespace, exc)


def reconcile_existing(namespace="default", kube_context=None,
                       service_host="", service_port=0):
    """Re-apply a tailnet node that **already exists**; never create one.

    What ``upgrade`` runs. Setup decides whether a cluster is on a tailnet; an upgrade
    carries a changed credential or serve config into one that already is, and must not
    turn the route on for a cluster that never asked -- an operator upgrading two clusters
    from one shell would otherwise publish the second by accident.

    Returns the hostname, or ``""`` when there is no node here.
    """
    from kubernetes import client  # noqa: PLC0415
    from kubernetes.client.exceptions import ApiException  # noqa: PLC0415

    from .kube_client import load_kube_config  # noqa: PLC0415

    load_kube_config(context=kube_context)
    try:
        client.AppsV1Api().read_namespaced_deployment(
            name=DEPLOYMENT_NAME, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ""
        raise
    return ensure_tailnet(namespace=namespace, kube_context=kube_context,
                          service_host=service_host, service_port=service_port,
                          enabled=True)


def published_hostname(namespace="default", kube_context=None):
    """The tailnet name this deployment answers on, or ``""`` when it has no node.

    What the service is reachable *as* is not a fact the service pod can look up -- it has
    no RBAC to read its own Deployments, deliberately -- so this is read by the CLI on the
    operator's behalf, the same way the published Ingress host is.
    """
    from kubernetes import client  # noqa: PLC0415
    from kubernetes.client.exceptions import ApiException  # noqa: PLC0415

    from .kube_client import load_kube_config  # noqa: PLC0415

    load_kube_config(context=kube_context)
    try:
        deployment = client.AppsV1Api().read_namespaced_deployment(
            name=DEPLOYMENT_NAME, namespace=namespace)
    except ApiException:
        return ""
    except Exception:  # noqa: BLE001 - reporting only; an unreadable cluster names nothing
        return ""
    containers = deployment.spec.template.spec.containers or []
    for container in containers:
        for env in (container.env or []):
            if getattr(env, "name", None) == "TS_HOSTNAME":
                return getattr(env, "value", "") or ""
    return ""
