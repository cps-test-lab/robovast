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

"""Check the prerequisites before something else discovers them the hard way.

Every check here exists because its absence used to surface as something unhelpful:
a missing ``helm`` as ``FileNotFoundError: 'helm'`` from a bare ``subprocess.run``, an
undersized node as a Kueue queue that never admits anything, a namespaced kubeconfig as
a 403 halfway through creating ClusterRoles — each of them minutes after the command
started and with the real cause several layers down.

Two rules shape it:

* **Every failure names its remedy.** A check that says "helm: missing" and stops has
  moved the problem, not solved it.
* **Nothing here changes anything.** It is safe to run at any time, which is what makes
  it usable as the first step of an install *and* as the first step of debugging one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the handshake type, for readers and type checkers only -- the runtime
    # imports below stay local, so a client-only install never pays for robovast.service.
    from robovast.service.interface import VersionInfo

#: Python this codebase requires (``pyproject.toml``: ``>=3.12,<3.14``).
MIN_PYTHON = (3, 12)

#: What the Kueue controller asks for; a cluster smaller than this never schedules it.
KUEUE_CPU = 4
KUEUE_MEMORY_GIB = 16


@dataclass
class Check:
    """One prerequisite, its verdict, and — when it failed — how to fix it."""

    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    #: A missing optional dependency is worth saying, but must not fail the run.
    optional: bool = False

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        return "warn" if self.optional else "FAIL"


def _tool(name: str, fix: str, *, optional: bool = False,
          version_args=("--version",)) -> Check:
    path = shutil.which(name)
    if not path:
        return Check(name, False, "not on PATH", fix, optional=optional)
    try:
        out = subprocess.run([name, *version_args], capture_output=True,  # noqa: S603
                             text=True, timeout=15, check=False)
        version = (out.stdout or out.stderr).strip().splitlines()
        detail = version[0][:60] if version else path
    except (OSError, subprocess.SubprocessError):
        detail = path
    return Check(name, True, detail)


def check_python() -> Check:
    current = sys.version_info[:2]
    if current >= MIN_PYTHON:
        return Check("python", True, ".".join(map(str, current)))
    return Check(
        "python", False, ".".join(map(str, current)),
        f"RoboVAST needs Python {'.'.join(map(str, MIN_PYTHON))} or newer; this "
        f"interpreter is {'.'.join(map(str, current))}. Recreate the venv with a "
        "newer interpreter (`make venv`).")


def check_tools(flavor: str = "") -> list[Check]:
    """The binaries the cluster paths shell out to."""
    checks = [
        # Each tool spells "tell me your version" differently, and `--version` is an
        # *error* for both of these — reporting that error as the version reads as a
        # broken install.
        _tool("kubectl", "Install kubectl: https://kubernetes.io/docs/tasks/tools/",
              version_args=("version", "--client=true")),
        _tool("helm", "Install helm: https://helm.sh/docs/intro/install/ — setup "
                      "installs Kueue with it.",
              version_args=("version", "--short")),
    ]
    if flavor == "gcp":
        checks.append(_tool(
            "gcloud",
            "Install the gcloud CLI and the GKE auth plugin "
            "(google-cloud-cli-gke-gcloud-auth-plugin); the gcp flavor uses them to "
            "authenticate and to size the Kueue quota."))
    checks.append(_tool("docker", "Install Docker — needed only to run campaigns on "
                                  "this machine, not for the cluster path.",
                        optional=True, version_args=("--version",)))
    return checks


def check_cluster(context: str | None = None) -> list[Check]:
    """Reachability, identity, and the permissions setup actually needs.

    Reports rather than raises, in every direction -- including "this install has no
    cluster support at all". The import is inside the ``try`` for that reason: it is the
    thing most likely to fail once the cluster lane ships as its own package, and a
    diagnostic command that dies while diagnosing is the one failure it cannot have.
    """
    try:
        from robovast.execution.cluster_execution.kube_client import \
            load_kube_config  # pylint: disable=import-outside-toplevel
        loaded = load_kube_config(context=context)
    except ImportError:
        # Not "to deploy or drive a cluster" any more: driving one is `vast exec cluster
        # run`, which this distribution ships. What needs the lane is OWNING a cluster --
        # deploying the service into it and operating it. Saying otherwise sent exactly
        # the audience this install is for after 290 MB they do not need.
        return [Check("cluster support", False, "not installed",
                      "This install has no cluster lane, and does not need one to run "
                      "campaigns ('vast exec cluster run' works). Install it to deploy "
                      "or operate a cluster of your own.", optional=True)]
    except Exception as exc:  # noqa: BLE001 - every other failure means "no cluster"
        return [Check("kubeconfig", False, str(exc)[:120],
                      "Point kubectl at a cluster (`kubectl config use-context …`), or "
                      "pass -x/--context.")]

    checks = [Check("kubeconfig", True, loaded)]

    from kubernetes import client  # pylint: disable=import-outside-toplevel

    from robovast.execution.cluster_execution.kube_client import \
        quiet_urllib3_retries  # pylint: disable=import-outside-toplevel

    # urllib3 prints a warning per retry attempt while it is still deciding whether the
    # call fails. Three of those ahead of a one-line verdict is exactly the noise this
    # command exists to replace.
    with quiet_urllib3_retries():
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
    """Setup creates ClusterRoles, which a namespace-scoped kubeconfig cannot.

    Unstated anywhere until now, and the point at which setup dies on a shared cluster.
    """
    from kubernetes import client
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
        "`vast exec cluster setup` creates cluster-scoped RBAC, so it needs a "
        "cluster-admin-ish kubeconfig. Ask an administrator to run setup, or to grant "
        "this subject cluster-admin.")


def _check_capacity() -> Check:
    """Kueue's controller asks for 4 CPU / 16 GiB; a smaller cluster never admits it."""
    from kubernetes import client
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

    # The largest single node, not the sum: the Kueue controller is one pod and has to
    # fit on one node. A cluster with plenty of total capacity and no node big enough
    # leaves it Pending forever, which is the failure this catches.
    best_cpu = max(_cpu(n.status.allocatable.get("cpu", "0")) for n in nodes)
    best_mem = max(_gib(n.status.allocatable.get("memory", "0")) for n in nodes)
    detail = f"largest node: {best_cpu:.1f} CPU, {best_mem:.1f} GiB"
    if best_cpu >= KUEUE_CPU and best_mem >= KUEUE_MEMORY_GIB:
        return Check("capacity", True, detail)
    return Check(
        "capacity", False, detail,
        f"Kueue's controller requests {KUEUE_CPU} CPU / {KUEUE_MEMORY_GIB} GiB and "
        "must fit on one node, so it would stay Pending here and no campaign would "
        "ever be admitted. Use a larger node.")


def check_deployment(namespace: str = "default",
                     context: str | None = None) -> list[Check]:
    """What the *cluster* intends for this deployment, as opposed to what the pod has.

    Two independent questions, so two Checks rather than one branch tree: is a push target
    configured (``build registry``), and can that target actually be reached
    (``registry route``). Named for the *infrastructure*, not the capability -- the
    client-side ``image builds`` check reports what the running service says it can do, and
    two rows with one name would read as a single check contradicting itself. Where they
    disagree, they are describing different things: a pod that predates its own registry
    config has the capability its config denies, and both lines printing is the point. The second only when the first is
    green — "the route is broken" is noise when there is no registry to route to.

    **The cluster import is deferred, and its absence is not an error.** This module ships
    in ``robovast-client``, which depends on neither the cluster lane nor the core, so on a
    client-only install ``robovast.execution`` does not exist in any form. A module-level
    import here would pass every core-without-lane test and break the install the
    distribution exists for. ``[]`` on ImportError, exactly as :func:`check_cluster` does.

    Silent when the lane is absent or the cluster unusable: :func:`check_cluster` has
    already said so, and saying it twice makes a reader look for two problems.

    All Checks are optional. A deployment that cannot build is not a broken install, and
    every verdict names the command that changes it.
    """
    try:
        from robovast.execution.cluster_execution import \
            service_deploy  # pylint: disable=import-outside-toplevel
    except ImportError:
        return []

    try:
        config_name, _kwargs = service_deploy.read_service_config_from_cluster(
            namespace, context)
    except Exception:  # noqa: BLE001 - check_cluster already reported an unusable cluster
        return []
    if not config_name:
        return [Check("build registry", False, f"no service in namespace {namespace!r}",
                      "Nothing is deployed here. Run 'vast exec cluster setup <flavor>', "
                      "or pass -n <namespace> if it is deployed elsewhere.",
                      optional=True)]

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
                "cannot push. 'vast exec cluster upgrade' re-bakes it from the live "
                "Ingress.", optional=True)]
        return [Check(
            "build registry", False, "not published, so no registry",
            "The registry is reached over the service's own Ingress, and there is none. "
            "Re-run 'vast exec cluster setup <flavor> --force --ingress-host <host>' with "
            "--issuer or --tls-secret (or --insecure-http on a trusted network).",
            optional=True)]

    checks = [Check("build registry", True, prefix)]
    checks.extend(_check_registry_route(namespace, context))
    checks.extend(_check_build_daemon(namespace, context))
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
        from robovast.execution.cluster_execution import buildkitd_deploy
        from robovast.execution.cluster_execution.kube_client import load_kube_config
    except ImportError:
        return []  # client-only install; see check_deployment's note

    try:
        load_kube_config(context)
        ready = buildkitd_deploy.buildkitd_ready(namespace)
    except Exception:  # noqa: BLE001 - an unreachable cluster is check_cluster's to report
        return []

    if ready:
        return [Check("build daemon", True, buildkitd_deploy.BUILDKITD_NAME)]
    return [Check(
        "build daemon", False, f"{buildkitd_deploy.BUILDKITD_NAME} has no ready pod",
        "Nothing can build until it is back: campaigns whose containers add packages are "
        "refused at submit. 'vast exec cluster upgrade' re-applies it. If it is there but "
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
        from kubernetes import client  # pylint: disable=import-outside-toplevel

        from robovast.execution.cluster_execution import \
            service_deploy  # pylint: disable=import-outside-toplevel
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
                  "'vast exec cluster upgrade' reconciles it.", optional=True)]


def check_client() -> list[Check]:
    """What a *user* needs: a service to talk to, and a command that reaches it.

    These come first because they are the only ones a person who will never deploy
    anything cares about, and because a green result here changes what the cluster
    prerequisites below *mean* — see :func:`run_checks`.
    """
    from robovast.client import login as login_config  # pylint: disable=import-outside-toplevel
    from robovast.client.service_target import \
        detected_service_url  # pylint: disable=import-outside-toplevel

    checks = []
    url, token, _name = login_config.credentials()
    if url and token:
        checks.append(Check("login", True, url))
    else:
        checks.append(Check(
            "login", False, "no stored credentials",
            "Run 'vast login <url>' with the URL and token your operator gave you. "
            "A local service prints both when it starts."))

    target = detected_service_url()
    if target:
        checks.append(Check("service", True, target))
    else:
        checks.append(Check(
            "service", False, "none answering",
            "Nothing is listening on the conventional local port and no stored login "
            "answers either. Start one with 'vast serve', or 'vast login <url>' to "
            "point at a running one."))

    if target:
        # Beside the `service` line above, because these describe the same subject -- and
        # one handshake answers both: a check per question was a second round trip to say
        # the same thing twice.
        info = _service_version(target)
        checks.extend(_check_service_revision(info))
        checks.extend(_check_build_capability(info))

    # Not `shutil.which`: this process may have a venv active that no other shell does,
    # which is exactly the case where the answer differs and the wrong one is reassuring.
    try:
        found = subprocess.run(["bash", "-lc", "command -v vast"],  # noqa: S603,S607
                               capture_output=True, text=True, timeout=15, check=False)
        resolved = found.stdout.strip() if found.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        resolved = ""
    if resolved:
        checks.append(Check("vast on PATH", True, resolved))
    else:
        checks.append(Check(
            "vast on PATH", False, "only inside this venv",
            "A new shell — an agent's, or your next terminal — cannot run 'vast'. "
            "Run 'vast login --link' to symlink it somewhere already on PATH."))

    return checks


def _service_version(target: str) -> "VersionInfo | None":
    """The service's version handshake, or ``None`` when it could not be read.

    ``None`` is "no verdict", never "no". A local ``vast serve`` whose token differs from
    the stored login answers 401, and a doctor that turned that into a red line would be
    reporting its own credential mismatch as the service's problem; an unreachable service
    is already the ``service`` check above, and saying it twice gives a reader two problems
    to chase where there is one.
    """
    from robovast.service.http_client import \
        RobovastClient  # pylint: disable=import-outside-toplevel
    try:
        return RobovastClient(target).version()
    except Exception:  # noqa: BLE001 - unreachable, unauthorised, or too old to ask
        return None


def _check_service_revision(info: "VersionInfo | None") -> list[Check]:
    """Whether the service is running the code this checkout has.

    The question a long-lived service makes real: it loads robovast **once, at startup**,
    so after an edit a perfectly reachable service may still be running the old code —
    and every symptom of that looks like a bug in the change. ``vast --version`` answers
    it for this side; until now nothing answered it for the service side outside the
    agent-only ``get_service_info``, so a human had to hand-roll an HTTP call.

    Three outcomes, and the third must stay distinct from the second:

    * equal — ✓, and nothing to do;
    * different — ⚠ with the command that rolls it, because *deploying* is the remedy;
    * **not reported** — the deployment cannot tell, which is not a mismatch. Reading it as
      one would send someone re-releasing to fix a service that may already be current.

    Both halves can be missing, and each silences a different thing: without a revision
    *here* there is nothing to compare against (report the service's and stop), and without
    one on either side there is nothing to say at all.

    Advisory throughout (``optional``): a service on a different revision from a working
    tree is the normal state of anyone mid-edit, so it must not fail the command.
    """
    if info is None:
        return []
    from robovast.client.app_version import \
        running_revision  # pylint: disable=import-outside-toplevel
    here = running_revision()
    deployed = getattr(info, "code_revision", "") or ""

    if not deployed:
        # Silent when this side has no revision either: the remedy for a service that
        # cannot report one is to re-release and roll it, which is only *anybody's* remedy
        # if they have the tree. Telling a client-only install to run `make release-images`
        # is a line about somebody else's job on a service that may be perfectly current.
        if not here:
            return []
        return [Check(
            "service revision", False, "not reported",
            "This deployment cannot say which revision it runs, so \"is my change "
            "loaded?\" has no answer from it — probe for the behaviour instead. Images "
            "built before the revision was baked in report nothing: re-release the family "
            "('make release-images PROJECT=<registry> PUSH=1') and roll onto it "
            "('vast exec cluster upgrade') to get the answer back.",
            optional=True)]

    if not here:
        # Nothing to compare against is not a mismatch, and not a defect either: a
        # client-only or non-git install is a perfectly good one. Report what the service
        # said and stop.
        return [Check("service revision", True, f"{deployed} (nothing here to compare it to)")]
    if here == deployed:
        detail = f"{deployed} (matches this checkout)"
        if deployed.endswith("+dirty"):
            # Equal strings, possibly different code: the marker records *that* a tree was
            # dirty, and cannot distinguish two dirty trees. Saying "matches" flatly here
            # would be the one place this check lies.
            detail = f"{deployed} (matches this checkout — but '+dirty' cannot tell two " \
                     "dirty trees apart)"
        return [Check("service revision", True, detail)]
    return [Check(
        "service revision", False, f"{deployed} deployed, {here} here",
        "The service loaded its code at startup, so nothing edited since then is in it. "
        "Roll it onto this revision: 'make release-images PROJECT=<registry> PUSH=1' then "
        "'vast exec cluster upgrade' for a cluster, or restart 'vast serve' for a local "
        "one. Expected, and fine, when you are pointed at someone else's deployment.",
        optional=True)]


def _check_build_capability(info: "VersionInfo | None") -> list[Check]:
    """What the *running* service says about building images, from the handshake.

    Answered without kubectl, so a user who will never deploy anything still learns that
    the service they are pointed at cannot build — before authoring a container that adds
    packages and finding out from ``start_campaign``, after a push and a workspace.

    Silence in three cases, all of which are "no verdict" rather than "no":

    * the service did not report the field (older than it) — ``None`` must never be read
      as ``False``, or every healthy pre-field deployment gets told to fix itself;
    * the handshake could not be read at all (``info`` is ``None``; see
      :func:`_service_version`);
    * the service can build, and there is nothing to say beyond ✓.

    Optional, because a service with no registry is not a broken install — it is a
    deployment that cannot do one thing, and the operator half below says which command
    fixes it.
    """
    if info is None:
        return []
    if info.can_build_images is None:
        return []
    if info.can_build_images:
        return [Check("image builds", True, "available")]
    return [Check("image builds", False, "unavailable on this service",
                  info.build_unavailable or
                  "The service did not say why. 'vast doctor -n <namespace>' from a "
                  "machine with a kubeconfig reports which remedy applies.",
                  optional=True)]


def run_checks(flavor: str = "", context: str | None = None,
               namespace: str = "default") -> list[Check]:
    """Every check, in the order the two roles hit them.

    The cluster prerequisites are **advisory when the client checks pass**. They are what
    you need to *deploy* RoboVAST, not to use one, and a user with a working login and no
    kubectl is not broken — reporting them as failures told exactly the person who had
    nothing left to do that four things were wrong, and exited non-zero saying so.

    When the client half is not working, they stay fatal: then deploying is the likely
    intent, and a missing ``helm`` really does stop it.
    """
    client = check_client()
    # Optional checks are advisory by definition (`Check.optional`), so one failing must
    # not decide this. It decides whether the *operator* half is reported as advisory or
    # fatal, so counting an optional client failure here would turn "no registry
    # configured" into four red ✗ for kubectl, helm and a kubeconfig the user will never
    # need -- the exact confusion the client/operator split exists to prevent.
    usable = all(c.ok for c in client if not c.optional)
    cluster = check_cluster(context)
    operator = [check_python(), *check_tools(flavor), *cluster]
    # Only when the cluster is actually usable. Asking a deployment about itself over an
    # unreachable API server produces a second way of saying "no cluster", and a reader
    # then has two problems to chase where there is one.
    if all(c.ok for c in cluster):
        operator += check_deployment(namespace=namespace, context=context)
    if usable:
        operator = [replace(c, optional=True) for c in operator]
    return client + operator
