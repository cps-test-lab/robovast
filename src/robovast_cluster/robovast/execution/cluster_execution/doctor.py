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

"""``vast doctor``'s cluster checks: the tools, the cluster, and the deployment in it.

Registered in the ``robovast.doctor_checks`` entry-point group; the command, the ordering
and the advisory/fatal split are :mod:`robovast.client.doctor`'s.
"""

from __future__ import annotations

from kubernetes import client

from robovast.client.doctor import Check, DoctorOptions, tool_check
from robovast.execution.cluster_execution import (buildkitd_deploy, kube_client,
                                                  node_placement, service_deploy)


def doctor_checks(options: DoctorOptions) -> list[Check]:
    """The tools, then the cluster, then -- only on a usable cluster -- the deployment.

    Asking a deployment about itself over an unreachable API server is a second way of
    saying "no cluster", and a reader then has two problems to chase where there is one.
    """
    cluster = check_cluster(options.context)
    checks = [*check_tools(options.flavor), *cluster]
    if all(c.ok for c in cluster):
        checks += check_deployment(namespace=options.namespace, context=options.context)
    return checks


def check_tools(flavor: str = "") -> list[Check]:
    """The binaries the cluster paths shell out to."""
    checks = [
        # Each tool spells "tell me your version" differently, and `--version` is an
        # *error* for both of these — reporting that error as the version reads as a
        # broken install.
        tool_check("kubectl", "Install kubectl: https://kubernetes.io/docs/tasks/tools/",
                   version_args=("version", "--client=true")),
        tool_check("helm", "Install helm: https://helm.sh/docs/intro/install/ — setup "
                           "installs the GPU device plugin with it.",
                   version_args=("version", "--short")),
    ]
    if flavor == "gcp":
        checks.append(tool_check(
            "gcloud",
            "Install the gcloud CLI and the GKE auth plugin "
            "(google-cloud-cli-gke-gcloud-auth-plugin); the gcp flavor uses them to "
            "authenticate and to read the autoscaler's maximum size."))
    return checks


def check_cluster(context: str | None = None) -> list[Check]:
    """Reachability, identity, and the permissions setup actually needs.

    Reports rather than raises: a diagnostic command that dies while diagnosing is the one
    failure it cannot have.
    """
    try:
        loaded = kube_client.load_kube_config(context=context)
    except Exception as exc:  # noqa: BLE001 - every failure here means "no cluster"
        return [Check("kubeconfig", False, str(exc)[:120],
                      "Point kubectl at a cluster (`kubectl config use-context …`), or "
                      "pass -x/--context.")]

    checks = [Check("kubeconfig", True, loaded)]

    # urllib3 prints a warning per retry attempt while it is still deciding whether the
    # call fails. Three of those ahead of a one-line verdict is exactly the noise this
    # command exists to replace.
    with kube_client.quiet_urllib3_retries():
        try:
            version = client.VersionApi().get_code()
            checks.append(Check("cluster", True,
                                f"Kubernetes {version.major}.{version.minor}"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(
                "cluster", False, f"{type(exc).__name__}: unreachable",
                "The API server did not answer. Check the cluster is running and "
                "reachable (VPN, `kubectl cluster-info`), or select another context "
                "with -x."))
            return checks

        checks.append(_check_rbac())
        checks.append(_check_capacity())
    return checks


def _check_rbac() -> Check:
    """Setup creates ClusterRoles, which a namespace-scoped kubeconfig cannot."""
    review = {"spec": {"resourceAttributes": {
        "group": "rbac.authorization.k8s.io", "resource": "clusterroles",
        "verb": "create"}}}
    try:
        result = client.AuthorizationV1Api().create_self_subject_access_review(review)
    except Exception as exc:  # noqa: BLE001
        return Check("permissions", False, str(exc)[:120],
                     "Could not check permissions; setup needs to create ClusterRoles.")
    if result.status.allowed:
        return Check("permissions", True, "can create ClusterRoles")
    return Check(
        "permissions", False, "cannot create ClusterRoles",
        "`vast cluster setup` creates cluster-scoped RBAC, so it needs a "
        "cluster-admin-ish kubeconfig. Ask an administrator to run setup, or to grant "
        "this subject cluster-admin.")


def _check_capacity() -> Check:
    """Report the largest node, and fail only on a cluster that can run nothing.

    There is no honest fixed threshold to check against: a campaign's pod is whatever its
    ``.vast`` asks for, and admission refuses an oversized request at launch, naming the
    request and each node's allocatable -- a better answer than any number guessed here.
    A cluster that could not run a single container is broken rather than small.
    """
    try:
        nodes = client.CoreV1Api().list_node().items
    except Exception as exc:  # noqa: BLE001
        return Check("capacity", False, str(exc)[:120],
                     "Could not read node capacity.")
    if not nodes:
        return Check("capacity", False, "no nodes", "The cluster reports no nodes.")

    def _cpu(value: str) -> float:
        return float(value[:-1]) / 1000 if value.endswith("m") else float(value)

    def _gib(value: str) -> float:
        units = {"Ki": 1 / 1024 / 1024, "Mi": 1 / 1024, "Gi": 1.0, "Ti": 1024.0}
        for suffix, factor in units.items():
            if value.endswith(suffix):
                return float(value[:-len(suffix)]) * factor
        return float(value) / (1024 ** 3)

    # The largest single node, not the sum: a pod runs on one node, so total capacity
    # spread thinly is what a cluster with "plenty of room" and nothing schedulable looks
    # like -- and it is the number an operator needs when admission refuses a request.
    best_cpu = max(_cpu(n.status.allocatable.get("cpu", "0")) for n in nodes)
    best_mem = max(_gib(n.status.allocatable.get("memory", "0")) for n in nodes)
    detail = f"largest node: {best_cpu:.1f} CPU, {best_mem:.1f} GiB"
    if best_cpu > 0 and best_mem > 0:
        return Check("capacity", True, detail)
    return Check(
        "capacity", False, detail,
        "No node reports allocatable CPU and memory, so nothing can be scheduled here "
        "at all. Check that the nodes are Ready ('kubectl get nodes').")


def check_deployment(namespace: str = "default",
                     context: str | None = None) -> list[Check]:
    """What the *cluster* intends for this deployment, as opposed to what the pod has.

    Two independent questions, so two Checks rather than one branch tree: is a push target
    configured (``build registry``), and can that target actually be reached
    (``registry route``). Named for the *infrastructure*, not the capability -- the
    client-side ``image builds`` check reports what the running service says it can do, and
    two rows with one name would read as a single check contradicting itself. Where they
    disagree, they are describing different things: a pod that predates its own registry
    config has the capability its config denies, and both lines printing is the point. The
    second only when the first is green — "the route is broken" is noise when there is no
    registry to route to.

    Silent when the cluster is unusable: :func:`check_cluster` has already said so, and
    saying it twice makes a reader look for two problems.

    All Checks are optional. A deployment that cannot build is not a broken install, and
    every verdict names the command that changes it.
    """
    try:
        config_name, _kwargs = service_deploy.read_service_config_from_cluster(
            namespace, context)
    except Exception:  # noqa: BLE001 - check_cluster already reported an unusable cluster
        return []
    if not config_name:
        return [Check("build registry", False, f"no service in namespace {namespace!r}",
                      "Nothing is deployed here. Run 'vast cluster setup <flavor>', "
                      "or pass -n <namespace> if it is deployed elsewhere.",
                      optional=True)]

    return (_check_build_registry(namespace, context)
            + _check_job_placement(namespace, context))


def _check_build_registry(namespace: str, context: str | None) -> list[Check]:
    """The ``build registry`` row and, when it is green, the route and daemon rows.

    See :func:`check_deployment`; the service is known to exist here.
    """
    try:
        prefix = service_deploy.deployed_registry_prefix(namespace, context)
        host = service_deploy.published_host(namespace, context)
    except Exception:  # noqa: BLE001 - same reason as above
        return []

    if not prefix:
        # The two states the in-pod service cannot tell apart -- and from here, with the
        # Ingress readable, they *can* be. Which is the whole reason this check exists.
        if host:
            return [Check(
                "build registry", False, f"no prefix (published at {host})",
                "The service is published but its registry prefix is unset, so builds "
                "cannot push. 'vast service upgrade' re-bakes it from the live "
                "Ingress.", optional=True)]
        return [Check(
            "build registry", False, "not published, so no registry",
            "The registry is reached over the service's own Ingress, and there is none. "
            "Re-run 'vast cluster setup <flavor> --force --ingress-host <host>' with "
            "--issuer or --tls-secret (or --insecure-http on a trusted network).",
            optional=True)]

    checks = [Check("build registry", True, prefix)]
    checks.extend(_check_registry_route(namespace, context))
    checks.extend(_check_build_daemon(namespace, context))
    return checks


def _check_job_placement(namespace: str, context: str | None) -> list[Check]:
    """Whether campaign jobs have anywhere to go, and every job node alias somewhere to point.

    :func:`_check_capacity` lists every node, so a job node pool that matches none -- a
    typo in ``ROBOVAST_JOB_NODE_LABELS``, a relabelled node pool -- still reads green there while
    admission counts zero capacity and no campaign ever starts. The pool is read from the
    live Deployment, the only place it is recorded.

    Each registered alias is resolved exactly as a campaign that names it would be, so a
    red row here is the refusal that campaign would get: unregistered, on several nodes,
    unschedulable, outside the pool, or on a node with no identity label. Not optional:
    either fault stops campaigns, unlike a deployment that merely cannot build. The node
    is shown beside its alias -- this command is run by the operator who registered it.
    """
    try:
        kube_client.load_kube_config(context)
        pool = service_deploy.job_node_pool_from_cluster(namespace, context)
    except ValueError as exc:
        return [Check("job node pool", False, str(exc)[:120],
                      "The service's recorded pool cannot be parsed, so admission refuses "
                      "to guess. Fix ROBOVAST_JOB_NODE_LABELS in the deployment's .env "
                      "and run 'vast service upgrade'.")]
    except Exception:  # noqa: BLE001 - an unreachable cluster is check_cluster's to report
        return []

    described = ", ".join(f"{k}={v}" for k, v in pool.items()) or "every node"
    try:
        core = client.CoreV1Api()
        matching = core.list_node(
            label_selector=",".join(f"{k}={v}" for k, v in pool.items()) or None).items
        eligible = node_placement.eligible_nodes(
            core, node_placement.CAMPAIGN_NODE_TOLERATIONS, extra_labels=pool)
        registry = node_placement.registered_aliases(core)
    except Exception:  # noqa: BLE001 - same reason as above
        return []

    if not matching:
        checks = [Check(
            "job node pool", False, f"{described}: matches no node",
            "Admission counts capacity only inside the pool, so no campaign job can start. "
            "'kubectl get nodes --show-labels' shows what the nodes carry; set "
            "ROBOVAST_JOB_NODE_LABELS in the deployment's .env to a pool that matches and run "
            "'vast service upgrade'.")]
    elif not eligible:
        checks = [Check(
            "job node pool", False,
            f"{described}: {len(matching)} node(s), none schedulable",
            "Every node in the pool is cordoned, not Ready, or carries a taint a job pod "
            "does not tolerate, so no campaign job can start. 'kubectl get nodes' shows "
            "which.")]
    else:
        checks = [Check("job node pool", True,
                        f"{described}: {len(eligible)} schedulable node(s)")]

    for alias, nodes in registry.items():
        name = f"job node alias {alias}"
        try:
            node_placement.resolve_job_node_alias(core, alias, pool=pool)
        except node_placement.AliasUnresolved as exc:
            checks.append(Check(
                name, False, f"{exc.cause} ({', '.join(nodes)})",
                f"{exc} Campaigns naming it are refused. Correct or remove it in "
                f"{node_placement.JOB_NODE_ALIASES_ENV} and run 'vast service upgrade "
                "--no-restart' to reconcile the labels."))
        except Exception:  # noqa: BLE001 - an unreadable node list is not a verdict
            continue
        else:
            checks.append(Check(name, True, nodes[0]))
    return checks


def _check_build_daemon(namespace: str, context: str | None) -> list[Check]:
    """Whether there is anything to build *with*.

    Images are solved by one long-lived BuildKit daemon rather than by a builder spawned inside
    each build pod, which is what lets the base image stay pulled and the pip download cache
    survive between builds. It is also a component that can be absent -- and when it is, every
    campaign that builds is refused at submit. That refusal names it, but a deployment should
    be able to find out before a campaign does.

    Reported next to the registry checks because it is the same kind of fact: infrastructure a
    build needs, that the service cannot repair for itself.
    """
    try:
        kube_client.load_kube_config(context)
        ready = buildkitd_deploy.buildkitd_ready(namespace)
    except Exception:  # noqa: BLE001 - an unreachable cluster is check_cluster's to report
        return []

    if ready:
        return [Check("build daemon", True, buildkitd_deploy.BUILDKITD_NAME)]
    return [Check(
        "build daemon", False, f"{buildkitd_deploy.BUILDKITD_NAME} has no ready pod",
        "Nothing can build until it is back: campaigns whose containers add packages are "
        "refused at submit. 'vast service upgrade' re-applies it. If it is there but "
        "not ready, its store may be on a node it is pinned to and cannot reach -- check "
        f"'kubectl -n {namespace} describe deploy/{buildkitd_deploy.BUILDKITD_NAME}'.",
        optional=True)]


def _check_registry_route(namespace: str, context: str | None) -> list[Check]:
    """Whether the configured push target is actually reachable.

    Read from the Ingress object rather than by probing ``GET /v2/``, because the object
    says more: a probe cannot see a missing ``proxy-body-size`` annotation, and without it
    every layer push dies on nginx's 1 MiB default with a 413 while ``/v2/`` answers 200.
    A probe from a workstation also proves little about the failure that matters — the
    resolver and trust store deciding whether a *node* can pull are not this machine's.
    """
    try:
        ingress = client.NetworkingV1Api().read_namespaced_ingress(
            service_deploy.SERVICE_NAME, namespace)
        defects = service_deploy.registry_ingress_defects(ingress)
    except Exception:  # noqa: BLE001 - unreadable Ingress is not a verdict about the route
        return []
    if not defects:
        return [Check("registry route", True, "reachable")]
    return [Check("registry route", False, "; ".join(defects),
                  "The registry has a prefix but the Ingress does not route to it "
                  "correctly, so pushes fail even though builds start. "
                  "'vast service upgrade' reconciles it.", optional=True)]
