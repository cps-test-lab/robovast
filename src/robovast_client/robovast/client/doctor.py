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

Every check exists because without it the fault surfaces as something unhelpful: a
missing binary as ``FileNotFoundError`` from a bare ``subprocess.run``, a cluster that can
schedule nothing as jobs that never start — each of them minutes after the command
started and with the real cause several layers down.

Two rules shape it:

* **Every failure names its remedy.** A check that says "helm: missing" and stops has
  moved the problem, not solved it.
* **Nothing here changes anything.** It is safe to run at any time, which is what makes
  it usable as the first step of an install *and* as the first step of debugging one.

This module holds what the client itself needs: Python, the login, the service and its
handshake. What another distribution needs — Docker for the core, the Kubernetes tools and
the cluster for ``robovast-cluster`` — it registers in the :data:`CHECK_GROUP` entry-point
group, as a callable taking :class:`DoctorOptions` and returning a list of :class:`Check`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, distribution, entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the handshake type, for readers and type checkers only -- the runtime
    # imports below stay local, so a client-only install never pays for robovast.service.
    from robovast.service.interface import VersionInfo

#: Python this codebase requires (``pyproject.toml``: ``>=3.12,<3.14``).
MIN_PYTHON = (3, 12)

#: The entry-point group other distributions contribute their checks through.
CHECK_GROUP = "robovast.doctor_checks"

#: The distributions that register in :data:`CHECK_GROUP`. One that is installed and
#: registered nothing has stale entry-point metadata, which would otherwise read as
#: "nothing to check".
CHECK_PROVIDERS = ("robovast", "robovast-cluster")


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


@dataclass(frozen=True)
class DoctorOptions:
    """What ``vast doctor`` was asked to check, as every check plugin receives it."""

    #: A cluster flavor whose extra needs to check (``gcp``), or ``""``.
    flavor: str = ""
    #: The kubeconfig context, or ``None`` for the active one.
    context: str | None = None
    #: The namespace the service is deployed in.
    namespace: str = "default"


def tool_check(name: str, fix: str, *, optional: bool = False,
               version_args=("--version",)) -> Check:
    """Whether the binary ``name`` is on PATH, with its version as the detail."""
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


def check_client() -> list[Check]:
    """What a *user* needs: a service to talk to, and a command that reaches it.

    These come first because they are the only ones a person who will never deploy
    anything cares about, and because a green result here changes what the operator
    prerequisites after them *mean* — see :func:`run_checks`.
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
            "Run 'vast login <url>' with the URL and token your operator gave you."))

    target = detected_service_url()
    # Handshake FIRST, because the row below claims the service is answering and must not
    # say so on the strength of a configured URL alone. A stored login is configuration,
    # not reachability: with the pod mid-roll that prints "\u2713 service" while every call
    # times out, and the revision and image-build rows silently vanish rather than
    # reporting a fault -- a green tick and two missing lines for a service that is down.
    info, err = _service_version(target) if target else (None, None)
    if target and _service_answered(err):
        checks.append(Check("service", True, target))
    elif target:
        checks.append(Check(
            "service", False, f"{target} not answering",
            f"The URL is configured but nothing replied ({type(err).__name__}). If this is "
            "a cluster, the pod may be mid-roll or down: 'vast service upgrade' after "
            "it settles, or check the ingress."))
    else:
        checks.append(Check(
            "service", False, "none answering",
            "Nothing is listening on the conventional local port and no stored login "
            "answers either. 'vast login <url>' points at a deployed service; "
            "'vast cluster setup' deploys one."))

    if target:
        # Beside the `service` line above, because these describe the same subject -- and
        # one handshake answers both: a check per question was a second round trip to say
        # the same thing twice.
        checks.extend(_check_service_revision(info, err))
        checks.extend(_check_build_capability(info, err))

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


def _service_version(target: str) -> "tuple[VersionInfo | None, Exception | None]":
    """``(handshake, error)`` — the service's version, and why it could not be read.

    The error is returned rather than swallowed because two very different faults otherwise
    arrive as the same ``None``: a service that ANSWERED and refused (a local ``vast serve``
    whose token differs from the stored login answers 401) and one that could not be reached
    at all. Only the first is "no verdict"; the second is the ``service`` check's verdict,
    which it cannot reach if this function keeps the evidence to itself.

    A refusal is a :class:`ServiceError` carrying ``.status``, so "it answered" is decidable
    — see :func:`_service_answered`.
    """
    from robovast.service.http_client import \
        RobovastClient  # pylint: disable=import-outside-toplevel
    try:
        return RobovastClient(target).version(), None
    except Exception as e:  # noqa: BLE001 - unreachable, unauthorised, or too old to ask
        return None, e


def _service_answered(err: "Exception | None") -> bool:
    """Did the service reply at all, whatever it replied?

    An HTTP status means yes: it was reached, parsed the request and refused it. No status
    means the request never got an answer — DNS, TCP, TLS, a timeout, a pod mid-roll.
    """
    return err is None or getattr(err, "status", None) is not None


def _check_service_revision(info: "VersionInfo | None",
                            err: "Exception | None" = None) -> list[Check]:
    """Whether the service is running the code this checkout has.

    The question a long-lived service makes real: it loads robovast **once, at startup**,
    so after an edit a perfectly reachable service may still be running the old code —
    and every symptom of that looks like a bug in the change. ``vast --version`` answers
    it for this side, this row for the service side.

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
        # Silent ONLY when the service never answered: the `service` row above is red and
        # names it, and two rows for one fault sends a reader chasing twice. But a service
        # that ANSWERED and refused the handshake is a different, unreported thing --
        # disappearing for it too would make a 401 read as "no revision question exists"
        # rather than "your credentials cannot ask it".
        if not _service_answered(err):
            return []
        status = getattr(err, "status", None)
        detail = getattr(err, "detail", "") or type(err).__name__
        return [Check(
            "service revision", False, f"could not be read (HTTP {status})",
            f"The service answered but refused the version handshake: {detail}. A 401/403 "
            "is usually a token that does not match this deployment — 'vast login <url>' "
            "with the token it printed. Until then \"is my change loaded?\" has no answer.",
            optional=True)]
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
            "('vast service upgrade') to get the answer back.",
            optional=True)]

    if not here:
        # Nothing to compare against is not a mismatch, and not a defect either: a
        # client-only or non-git install is a perfectly good one. Report what the service
        # said and stop.
        return [Check("service revision", True, f"{deployed} (nothing here to compare it to)")]
    here_sha, here_dirty = _split_revision(here)
    deployed_sha, deployed_dirty = _split_revision(deployed)

    if _same_commit(here_sha, deployed_sha):
        if here_dirty or deployed_dirty:
            # Same commit, and at least one side has uncommitted changes on top of it. Not
            # a match to assert and not a mismatch to report: the marker records *that* a
            # tree was dirty and can say nothing about what is in it, so the honest row
            # names the commit they share and stops short of claiming the code is equal.
            whose = ("both sides are" if here_dirty and deployed_dirty
                     else "this checkout is" if here_dirty else "the deployment is")
            return [Check(
                "service revision", True,
                f"{deployed_sha} (same commit, but {whose} '+dirty' — the marker cannot "
                "say what is on top of it)")]
        return [Check("service revision", True, f"{deployed} (matches this checkout)")]

    return [Check(
        "service revision", False, f"{deployed} deployed, {here} here",
        "The service loaded its code at startup, so nothing edited since then is in it. "
        "Roll it onto this revision: 'make release-images PROJECT=<registry> PUSH=1' then "
        "'vast service upgrade'. Expected, and fine, when you are pointed at someone "
        "else's deployment.",
        optional=True)]


def _split_revision(revision: str) -> tuple:
    """``<short-sha>[+dirty]`` -> ``(sha, dirty)``."""
    sha, _, suffix = revision.partition("+")
    return sha, suffix == "dirty"


def _same_commit(here_sha: str, deployed_sha: str) -> bool:
    """Do two abbreviated shas name the same commit?

    Prefix comparison, not equality, because **the two sides abbreviate independently**:
    the deployment's is baked into the image by whatever produced it and this one comes
    from the local checkout, so the same commit routinely arrives as ``a9c955a`` and
    ``a9c955a7``. Compared with ``==`` that reads as a mismatch and sends someone
    re-releasing a service that is already current -- a false alarm from the one check
    whose entire job is answering "is my change loaded?".

    Guarded on length because a prefix test is only as trustworthy as the shorter string:
    git never abbreviates below 4, and anything shorter here is malformed rather than
    short, so it is not treated as naming a commit at all.
    """
    shorter, longer = sorted((here_sha, deployed_sha), key=len)
    return len(shorter) >= 4 and longer.startswith(shorter)


def _check_build_capability(info: "VersionInfo | None",
                            err: "Exception | None" = None) -> list[Check]:
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
    deployment that cannot do one thing, and the cluster plugin's deployment rows say which
    command fixes it.
    """
    if info is None:
        # Same split as the revision row: nothing to add when the service never answered,
        # but an answered-and-refused handshake means this capability is unknown rather
        # than absent, and silence read as "nothing to report about building".
        if not _service_answered(err):
            return []
        return [Check("image builds", False, "unknown — the handshake was refused",
                      "Whether this service can build images is part of the version "
                      "handshake, which it would not answer. Fix the credentials (see the "
                      "revision row) and re-run.",
                      optional=True)]
    if info.can_build_images is None:
        return []
    if info.can_build_images:
        return [Check("image builds", True, "available")]
    return [Check("image builds", False, "unavailable on this service",
                  info.build_unavailable or
                  "The service did not say why. 'vast doctor -n <namespace>' from a "
                  "machine with a kubeconfig reports which remedy applies.",
                  optional=True)]



def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed(dist_name: str) -> bool:
    """Whether a distribution is installed, read from its metadata without importing it."""
    try:
        distribution(dist_name)
    except PackageNotFoundError:
        return False
    return True


def plugin_checks(options: DoctorOptions) -> tuple[list[Check], list[Check]]:
    """``(checks, faults)``: what the installed plugins report, and what went wrong running them.

    A plugin that raises or returns something other than a list of :class:`Check` is a
    fault naming the plugin rather than a crash — a diagnostic command that dies while
    diagnosing is the one failure it cannot have. So is a provider in
    :data:`CHECK_PROVIDERS` that is installed and registered nothing.
    """
    checks: list[Check] = []
    faults: list[Check] = []
    contributed = set()
    for ep in sorted(entry_points(group=CHECK_GROUP), key=lambda e: e.name):
        dist = getattr(ep, "dist", None)
        if dist is not None:
            contributed.add(_canonical(dist.metadata["Name"] or ""))
        try:
            result = list(ep.load()(options))
            if not all(isinstance(c, Check) for c in result):
                raise TypeError("returned something other than a list of Check")
        except Exception as exc:  # noqa: BLE001 - reported as the plugin's fault
            faults.append(Check(
                f"{ep.name} checks", False, f"{type(exc).__name__}: {exc}"[:120],
                f"The doctor plugin '{ep.name}' ({ep.value}) failed rather than reporting. "
                "Its distribution is broken or out of date: reinstall it ('make venv' in "
                "a checkout)."))
            continue
        checks.extend(result)

    for provider in CHECK_PROVIDERS:
        if provider not in contributed and _installed(provider):
            faults.append(Check(
                f"{provider} checks", False, "installed, but registered none",
                f"The entry points of {provider} are stale, so its prerequisites were not "
                "checked. Re-run 'pip install -e .' for it (or 'make venv') in the "
                "checkout."))
    return checks, faults


def check_cluster_support() -> list[Check]:
    """One advisory line when ``robovast-cluster`` is not installed, else nothing.

    Its checks come from its own plugin; this says why there are none.
    """
    if _installed("robovast-cluster"):
        return []
    return [Check("cluster support", False, "not installed",
                  "This install has no cluster package, and does not need one to run "
                  "campaigns ('vast workspace run' works). Install it to deploy "
                  "or operate a cluster of your own.", optional=True)]


def run_checks(flavor: str = "", context: str | None = None,
               namespace: str = "default") -> list[Check]:
    """Every check, the client's first.

    The rest — Python and whatever the plugins report — are what you need to *deploy*
    RoboVAST, not to use one, so they are **advisory when the client checks pass**: a
    user with a working login and no kubectl is not broken. When the client half is not
    working, deploying is the likely intent and they stay fatal. A plugin fault stays
    fatal either way: it is a broken install, not a missing prerequisite.
    """
    client = check_client()
    # Optional checks are advisory by definition, so one failing must not decide whether
    # the operator half is advisory or fatal.
    usable = all(c.ok for c in client if not c.optional)
    options = DoctorOptions(flavor=flavor, context=context, namespace=namespace)
    contributed, faults = plugin_checks(options)
    operator = [check_python(), *contributed, *check_cluster_support()]
    if usable:
        operator = [replace(c, optional=True) for c in operator]
    return client + operator + faults
