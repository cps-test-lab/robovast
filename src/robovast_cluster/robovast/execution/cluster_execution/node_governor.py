# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Set the CPU frequency governor on the nodes that run campaigns.

**Why this exists at all**, since reconfiguring someone's hosts is not obviously a test
framework's business. A node on a scaling governor changes clock speed with load, so a
per-node figure a campaign records -- CPU usage, realtime factor, run duration -- is taken
against a clock that was not the same for every run. Fixing the governor removes that
variable. It does not claim to be the only one, and it is not a performance feature: what
it buys is comparability between runs, not speed.

A measurement taken while a node was quiet describes a state ordinary runs do not meet, and
a calibration probe is exactly that measurement -- it runs alone by design.

**On by default, and skippable by name.** Setting a host's power policy overrides the
operator's own decision about power, heat and cost on a machine RoboVAST is a guest on --
unlike advertising a GPU, which only reports a fact. It is still the default, because a
cluster used for measurement whose clock moves with load produces numbers that are wrong in
a way nothing downstream can detect or correct.

The cost of the default is bounded by the failure policy: a cluster that refuses the DaemonSet
gets a warning and carries on, exactly as a GPU-less cluster does with the device plugin. Only
an explicit ``--performance-governor`` turns that refusal into an error -- see
:func:`ensure_cpu_governor`.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: The governor a cluster used for measurement should be on.
PERFORMANCE = "performance"

DAEMONSET_NAME = "robovast-cpu-governor"

#: Where the kernel exposes the per-policy governor. Written for every policy, because a
#: setting applied to cpu0 alone leaves the rest of the machine scaling and the result looks
#: like a partial success rather than the no-op it is.
GOVERNOR_GLOB = "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"

#: Busybox: the work is one shell loop over sysfs, and pulling a larger image onto every
#: node to run ``echo`` would cost more than the thing it configures.
IMAGE = "busybox:1.36"

#: Re-asserted rather than written once. A node that reboots comes back on its configured
#: default, and a governor silently reverting is worse than one never set -- the campaign
#: would keep reporting figures from a machine nobody knew had changed underneath it.
REASSERT_SECONDS = 60


def _script(governor: str) -> str:
    """Write *governor* to every policy, then keep re-asserting it.

    Exits non-zero when no policy accepted the value, so the pod CrashLoopBackOffs instead
    of sitting Ready over a cluster that never changed. A DaemonSet reporting Ready while
    having done nothing is the failure this module exists to make impossible: it would leave
    the operator believing their measurements were taken on a fixed clock.
    """
    return (
        'set -e\n'
        'wrote=0\n'
        f'for f in {GOVERNOR_GLOB}; do\n'
        '  [ -w "$f" ] || continue\n'
        f'  echo {governor} > "$f" 2>/dev/null && wrote=1\n'
        'done\n'
        'if [ "$wrote" = 0 ]; then\n'
        f'  echo "no cpufreq policy accepted {governor}" >&2\n'
        '  echo "cpufreq may be absent (a VM, or a cloud node whose host owns the clock),"'
        ' >&2\n'
        '  echo "or the governor may be unavailable on this driver" >&2\n'
        '  exit 1\n'
        'fi\n'
        f'echo "set {governor} on $(ls -d {GOVERNOR_GLOB} 2>/dev/null | wc -l) policies"\n'
        'while true; do\n'
        f'  sleep {REASSERT_SECONDS}\n'
        f'  for f in {GOVERNOR_GLOB}; do\n'
        f'    [ -w "$f" ] && echo {governor} > "$f" 2>/dev/null || true\n'
        '  done\n'
        'done\n'
    )


def manifest(namespace: str, governor: str, node_selector: Optional[dict] = None) -> dict:
    """The DaemonSet. Privileged, and deliberately the only privileged thing RoboVAST runs.

    Writing ``/sys/devices/system/cpu/*/cpufreq`` needs the host's sysfs mounted writable,
    which needs privilege; there is no narrower capability that grants it. That is a real
    cost, and the reason ``--no-performance-governor`` exists -- but not a reason to default
    to it, because a cluster whose clock moves with load produces numbers nothing downstream
    can detect or correct. See the module docstring.

    *node_selector* confines it to the campaign node pool when one is configured, so a
    cluster that runs campaigns on a subset of its machines does not have the others
    reconfigured as a side effect of a RoboVAST setup.
    """
    pod_spec = {
        "tolerations": [{"operator": "Exists"}],
        "containers": [{
            "name": "governor",
            "image": IMAGE,
            "command": ["sh", "-c", _script(governor)],
            "securityContext": {"privileged": True},
            "volumeMounts": [{"name": "sys", "mountPath": "/sys"}],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                          "limits": {"cpu": "50m", "memory": "32Mi"}},
        }],
        "volumes": [{"name": "sys", "hostPath": {"path": "/sys"}}],
    }
    if node_selector:
        pod_spec["nodeSelector"] = dict(node_selector)
    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": DAEMONSET_NAME, "namespace": namespace,
                     "labels": {"app": DAEMONSET_NAME}},
        "spec": {
            "selector": {"matchLabels": {"app": DAEMONSET_NAME}},
            "template": {"metadata": {"labels": {"app": DAEMONSET_NAME}},
                         "spec": pod_spec},
        },
    }


#: Told apart from any other failure because the remedy is completely different: a cluster
#: that forbids privileged pods will never run this, and retrying does not help.
FORBIDDEN_HINTS = ("privileged", "podsecurity", "psp", "forbidden", "security context",
                   "securitycontext", "admission webhook", "violates")


def refusal_message(governor: str, detail: str, *, forbidden: bool) -> str:
    """What to tell an operator whose cluster will not take this.

    The two cases need different remedies, so they are not merged. *forbidden* is a cluster
    that refuses privileged pods -- the managed-Kubernetes case, where nothing RoboVAST can
    do will change it and the honest answer is "configure the node image instead". Anything
    else is a cluster that accepted the DaemonSet but errored, which is worth reporting
    verbatim rather than explaining away.
    """
    if forbidden:
        why = (
            "This needs a privileged pod with the host's /sys mounted writable, and this "
            "cluster refuses it. Managed Kubernetes (GKE, EKS, AKS) generally does, and "
            "there node auto-repair also replaces machines, so a governor applied to a node "
            "would not survive its replacement. Set it through the node image or the node's "
            "startup configuration instead."
        )
    else:
        why = (
            "The cluster accepted the request but the API call failed. Nothing about the "
            "governor is implied either way -- this is the deploy failing, not the setting "
            "being unavailable."
        )
    return (
        f"could not set the CPU governor to '{governor}': {detail}\n{why}\n"
        "Leaving it unset is supported: RoboVAST reports a 'cpu_governor_scaling' warning "
        "per campaign so the effect is visible in the results rather than silent. Re-run "
        "setup without --performance-governor to continue without it."
    )


def ensure_cpu_governor(apps_api, namespace: str, enabled: bool, *, explicit: bool = False,
                        node_selector: Optional[dict] = None,
                        dry_run: bool = False) -> bool:
    """Apply the governor DaemonSet, or remove it when *enabled* is false.

    A boolean, not a governor name. :data:`PERFORMANCE` is the only setting that serves the
    purpose -- a cluster used for measurement wants a clock that does not move -- and
    offering the choice would invite ``powersave`` (the thing being fixed) or a typo that
    lands as an unavailable governor on some drivers and a silent no-op on others.

    Returns whether the DaemonSet is now installed.

    *explicit* is whether the operator named ``--performance-governor``, and it sets the
    failure policy -- the same split :func:`~.kubernetes_gpu.ensure_nvidia_device_plugin`
    makes, for the same reason:

    * **Implicit** (the default). A cluster that refuses the DaemonSet gets a warning and
      setup carries on. Managed Kubernetes forbids privileged pods, and a default that
      failed there would make ``setup`` impossible on those clusters over an improvement
      nobody asked for.
    * **Explicit.** The same refusal raises. Someone who asked for a fixed clock and
      silently did not get one is worse off than someone who never asked, because they
      would now trust measurements taken on a scaling clock.

    Either way the outcome is visible: the return value says whether it is installed, and a
    campaign whose nodes still scale is reported by the ``cpu_governor_scaling`` advice, so
    the warning path never becomes a silent one.

    Removal is likewise explicit and total. Setup writes the cluster's configuration on
    every run, so omitting the flag takes the DaemonSet away rather than leaving a previous
    one in force; a governor still being held by a deployment nobody remembers configuring
    is the same class of surprise as one silently reverting.
    """
    from kubernetes.client.exceptions import ApiException  # noqa: PLC0415

    governor = PERFORMANCE
    if not enabled:
        try:
            apps_api.delete_namespaced_daemon_set(name=DAEMONSET_NAME, namespace=namespace)
            logger.info("Removed the CPU governor DaemonSet. Nodes keep the governor they "
                        "were last set to until something changes it -- a reboot returns "
                        "them to their own default.")
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Could not remove %s: %s", DAEMONSET_NAME, exc)
        return False

    body = manifest(namespace, governor, node_selector=node_selector)
    if dry_run:
        logger.info("[dry-run] would apply %s (%s)", DAEMONSET_NAME, governor)
        return True
    try:
        apps_api.create_namespaced_daemon_set(namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status == 409:
            apps_api.replace_namespaced_daemon_set(
                name=DAEMONSET_NAME, namespace=namespace, body=body)
        else:
            detail = str(getattr(exc, "body", None) or exc)
            forbidden = (exc.status in (401, 403)
                         or any(h in detail.lower() for h in FORBIDDEN_HINTS))
            message = refusal_message(governor, detail, forbidden=forbidden)
            if explicit:
                raise RuntimeError(message) from exc
            logger.warning("%s", message)
            return False
    logger.info("CPU governor DaemonSet applied: every%s node will be set to '%s'.",
                " selected" if node_selector else "", governor)
    return True
