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
        return [Check("cluster support", False, "not installed",
                      "This install has no cluster lane. Install it to deploy or drive "
                      "a cluster; nothing else here needs it.", optional=True)]
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


def check_client() -> list[Check]:
    """What a *user* needs: a service to talk to, and a command that reaches it.

    These come first because they are the only ones a person who will never deploy
    anything cares about, and because a green result here changes what the cluster
    prerequisites below *mean* — see :func:`run_checks`.
    """
    from robovast.common.cli import login as login_config  # pylint: disable=import-outside-toplevel
    from robovast.common.cli.service_target import \
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

    # Not `shutil.which`: this process may have a venv active that no other shell does,
    # which is exactly the case where the answer differs and the wrong one is reassuring.
    import subprocess  # pylint: disable=import-outside-toplevel
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


def run_checks(flavor: str = "", context: str | None = None) -> list[Check]:
    """Every check, in the order the two roles hit them.

    The cluster prerequisites are **advisory when the client checks pass**. They are what
    you need to *deploy* RoboVAST, not to use one, and a user with a working login and no
    kubectl is not broken — reporting them as failures told exactly the person who had
    nothing left to do that four things were wrong, and exited non-zero saying so.

    When the client half is not working, they stay fatal: then deploying is the likely
    intent, and a missing ``helm`` really does stop it.
    """
    client = check_client()
    usable = all(c.ok for c in client)
    operator = [check_python(), *check_tools(flavor), *check_cluster(context)]
    if usable:
        operator = [replace(c, optional=True) for c in operator]
    return client + operator
