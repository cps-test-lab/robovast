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

"""Make a cluster's NVIDIA GPUs schedulable, so a simulation container can render on one.

Offscreen MuJoCo rendering needs a DRI render node in the container. Without one the
simulator falls back to software rendering -- correct frames, an order of magnitude
slower -- so on a GPU cluster this is the difference between a sweep that finishes and one
that does not. Three things have to line up, and this module owns the first:

1. the node must *advertise* ``nvidia.com/gpu``, which needs a device plugin (here);
2. Kueue's ClusterQueue must cover that resource (:mod:`.kubernetes_kueue`);
3. the pod must request it and ask the container runtime for the ``graphics`` driver
   capability (:mod:`.kubernetes_backend`).

Deliberately opportunistic. A cluster with no GPU is not a problem to be reported, it is
the ordinary case, and setup must behave exactly as it did before on one. So detection
failing, or the plugin failing to come up, is a warning and not an error -- *unless* the
operator asked for GPUs explicitly, in which case silently giving them software rendering
would be the worse outcome.
"""

import logging
import os
import tempfile
import time

from kubernetes import client

from .kubernetes_kueue import _parse_resource, _run_helm, helm_release_exists

logger = logging.getLogger(__name__)

#: The extended resource a device plugin advertises, and the name pods request.
GPU_RESOURCE = "nvidia.com/gpu"

#: RKE2/k3s register this RuntimeClass when the NVIDIA container toolkit is present on a
#: node, and it is the only way a pod on such a cluster gets the driver injected. So its
#: presence is not a guess about hardware -- it is the precondition for the thing we emit.
NVIDIA_RUNTIME_CLASS = "nvidia"

NVIDIA_PLUGIN_NAMESPACE = "nvidia-device-plugin"
NVIDIA_PLUGIN_RELEASE = "nvidia-device-plugin"
NVIDIA_PLUGIN_CHART = "nvidia-device-plugin"
#: Located with ``--repo`` rather than ``helm repo add``: setup runs on an operator's own
#: machine, and adding a repo to their global helm config is a side effect nobody asked for.
NVIDIA_PLUGIN_REPO = "https://nvidia.github.io/k8s-device-plugin"
#: Pinned, like Kueue's chart. A floating version would change what a re-run installs.
NVIDIA_PLUGIN_VERSION = "0.17.1"

#: Time-slicing replicas advertised per physical GPU when the operator did not say.
#:
#: Chosen to sit *above* the cpu ceiling so GPU gating never becomes the binding
#: constraint: a three-container scenario job asks for roughly ten cores, so a 96-core node
#: admits about nine concurrent jobs, and 16 leaves clear headroom over that. A smaller
#: default would cap campaigns that run wide today.
#:
#: It is a concurrency cap and **not** a VRAM budget -- nothing in Kubernetes, in the
#: plugin, or in the driver partitions device memory for time-sliced sharing, so all N
#: renderers allocate from the same card first-come-first-served. Raising it is an
#: assertion about how much VRAM a trial needs, which is why it takes a flag.
DEFAULT_GPU_REPLICAS = 16

#: ``runtimeClassName`` on the plugin itself is load-bearing on RKE2: reading NVML needs the
#: driver, and nvidia is a registered runtime there rather than the default one.
#:
#: ``tolerations`` matter for a reason that is easy to miss: a device advertiser has to run
#: wherever the GPUs are, and the ResourceFlavor tolerates ``dedicated=batch:NoSchedule``.
#: A tainted GPU node would otherwise get no plugin pod, advertise nothing, and fail the
#: capacity wait with a message about the wrong thing entirely.
#:
#: ``renameByDefault: false`` is stated rather than assumed. Under ``true`` the node
#: advertises ``nvidia.com/gpu.shared``, and a ClusterQueue covering ``nvidia.com/gpu``
#: would then never admit anything -- a permanent hang with no error.
#:
#: The ``affinity`` override is the one setting that decides whether any of this works. The
#: chart's default requires a Node Feature Discovery label
#: (``feature.node.kubernetes.io/pci-10de.present``, ``nvidia.com/gpu.present``, ...), and on
#: a cluster without NFD no node carries one -- so the DaemonSet is created with
#: ``DESIRED 0``, the helm release cheerfully reports "deployed", and nothing whatsoever
#: advertises a GPU. Detection here already established the GPU through the ``nvidia``
#: RuntimeClass, which is a stronger signal on this class of cluster than a label nothing
#: installs, so the label requirement is replaced rather than satisfied.
#:
#: It has to be a *permissive* term and not ``{}``: the chart wraps the value in a Helm
#: ``with``, which treats an empty map as absent and falls back to the very default being
#: overridden -- observed on the target cluster, where ``affinity: {}`` was accepted into the
#: release's values and changed nothing. Matching ``kubernetes.io/os=linux`` is non-empty,
#: so it takes effect, and selects every node that could run the plugin at all. Running it on
#: a GPU-less node is harmless: it finds no devices and advertises nothing.
NVIDIA_PLUGIN_VALUES = """
runtimeClassName: nvidia
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/os
              operator: In
              values: ["linux"]
tolerations:
  - operator: Exists
config:
  default: default
  map:
    default: |-
      version: v1
      flags:
        migStrategy: none
      sharing:
        timeSlicing:
          renameByDefault: false
          failRequestsGreaterThanOne: true
          resources:
            - name: nvidia.com/gpu
              replicas: {replicas}
"""


def _ctx_helm(kube_context):
    return [f"--kube-context={kube_context}"] if kube_context else []


def format_plugin_values(replicas):
    """The chart values for *replicas* time-slicing replicas."""
    return NVIDIA_PLUGIN_VALUES.format(replicas=int(replicas))


def get_cluster_allocatable_gpus(kube_context=None):
    """Total ``nvidia.com/gpu`` allocatable across all nodes; ``0`` when none advertise it.

    ``allocatable`` rather than ``capacity``, matching how cpu/memory quota is sized -- it
    is what the scheduler will actually hand out.
    """
    from .kube_client import load_kube_config

    load_kube_config(context=kube_context)
    total = 0
    for node in client.CoreV1Api().list_node().items:
        allocatable = (node.status.allocatable or {}) if node.status else {}
        total += int(_parse_resource(allocatable.get(GPU_RESOURCE)))
    return total


def nvidia_runtime_class_present(kube_context=None):
    """Whether the cluster has a ``nvidia`` RuntimeClass; ``None`` when it cannot be read.

    Three-valued on purpose. "Absent" and "could not tell" must not collapse into the same
    answer, because they call for opposite responses and the difference is invisible in the
    result: a 403 from a service account without ``runtimeclasses`` access read as "no such
    RuntimeClass", so GPU pods quietly lost ``runtimeClassName`` and rendered in software
    with the device attached and the quota charged. See :func:`gpu_runtime_class_for`.
    """
    from .kube_client import load_kube_config

    load_kube_config(context=kube_context)
    try:
        classes = client.NodeV1Api().list_runtime_class().items
    except Exception as exc:  # noqa: BLE001 - "cannot tell" is its own answer
        logger.debug("Could not list RuntimeClasses (%s)", exc)
        return None
    return any(rc.metadata and rc.metadata.name == NVIDIA_RUNTIME_CLASS for rc in classes)


def gpu_runtime_class_for(kube_context=None):
    """The ``runtimeClassName`` a GPU pod should carry, or ``None`` to omit the field.

    * **Present** -> name it. Required wherever nvidia is a registered runtime rather than
      the default one, which is every RKE2/k3s cluster.
    * **Absent** -> omit it. A managed GPU node pool advertises ``nvidia.com/gpu`` with no
      such RuntimeClass, and naming one that does not exist makes the API server reject the
      pod outright -- trading a slow campaign for one that cannot start.
    * **Cannot tell** -> name it anyway, loudly. Of the two ways to be wrong this is the one
      that announces itself: a wrong name fails the pod immediately with a message saying so,
      while omitting it produces correct results many times slower and reports success. A
      silent slow answer is the failure this whole path exists to prevent, so it is not the
      one to default to.
    """
    present = nvidia_runtime_class_present(kube_context=kube_context)
    if present:
        return NVIDIA_RUNTIME_CLASS
    if present is None:
        logger.warning(
            "Could not read the cluster's RuntimeClasses, so whether '%s' exists is unknown; "
            "setting runtimeClassName anyway for the GPU pods. If those pods are rejected for "
            "a missing RuntimeClass, this cluster does not need it. Grant the service read "
            "access to runtimeclasses (node.k8s.io) -- 'vast exec cluster upgrade' does.",
            NVIDIA_RUNTIME_CLASS)
        return NVIDIA_RUNTIME_CLASS
    return None


def _deployed_replicas(kube_context=None):
    """The replica count currently deployed by *our* release, or ``None`` if unreadable.

    Read back so a bare re-run of ``setup`` does not silently undo a deliberate
    ``--gpu-replicas 24``. Same courtesy ``setup`` already extends to the access token,
    which is preserved across re-runs unless ``--rotate-token`` asks otherwise.
    """
    import json
    import subprocess

    result = subprocess.run(
        ["helm", "get", "values", NVIDIA_PLUGIN_RELEASE, "-n", NVIDIA_PLUGIN_NAMESPACE,
         "-o", "json"] + _ctx_helm(kube_context),
        capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode != 0:
        return None
    try:
        values = json.loads(result.stdout or "{}") or {}
        text = ((values.get("config") or {}).get("map") or {}).get("default") or ""
    except (ValueError, AttributeError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("replicas:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _wait_for_gpu_capacity(expected, kube_context=None, timeout=120):
    """Block until the cluster advertises *expected* GPUs; return the count, or raise.

    Asserts the exact number rather than "more than zero", and that is not pedantry:
    changing the time-slicing config rewrites the plugin's ConfigMap and restarts its
    DaemonSet, so capacity goes ``24 -> absent -> 16``. A check that accepts any non-zero
    reading can therefore see the *old* value and size the Kueue quota from it -- wrongly,
    permanently, and while reporting success.
    """
    deadline = time.monotonic() + timeout
    seen = 0
    while True:
        seen = get_cluster_allocatable_gpus(kube_context=kube_context)
        if seen >= expected:
            logger.info("Cluster advertises %d %s", seen, GPU_RESOURCE)
            return seen
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"The NVIDIA device plugin was installed but the cluster advertises "
                f"{seen} {GPU_RESOURCE} after {timeout}s (expected {expected}).\n"
                f"  Check it with: kubectl -n {NVIDIA_PLUGIN_NAMESPACE} logs "
                f"-l app.kubernetes.io/name={NVIDIA_PLUGIN_CHART}\n"
                f"  Common causes: the driver is not loaded on the node, the node has no "
                f"NVIDIA card, or a taint the plugin does not tolerate keeps its pod off "
                f"the GPU node.\n"
                f"  Pass --no-gpu to set the cluster up without GPU scheduling.")
        time.sleep(2)


def ensure_nvidia_device_plugin(kube_context=None, gpu_replicas=None, skip=False):
    """Make GPUs schedulable if this cluster has any. Returns the advertised count or ``None``.

    ``gpu_replicas`` is the operator's explicit request; ``None`` means "decide for me",
    which is the path a plain ``vast exec cluster setup`` takes. That distinction sets the
    failure policy, and it is the whole reason a GPU-less cluster keeps working exactly as
    before:

    * **Implicit.** Nothing found, or the plugin does not come up: warn and carry on
      without GPU. Setup must not fail because an optimisation nobody asked for did not
      pan out.
    * **Explicit.** The same conditions raise. Someone who passed ``--gpu-replicas``
      would otherwise get software rendering while believing they had a GPU.

    Either way nothing is left half-configured, and that is structural rather than
    promised: the Kueue quota is read from live node capacity *after* this returns, so a
    failed install advertises nothing, the queue covers no GPU, the job requests none, and
    every layer agrees.
    """
    requested = gpu_replicas is not None

    def _giveup(message):
        """Explicit request -> raise; opportunistic -> warn and fall through to no GPU."""
        if requested:
            raise RuntimeError(message)
        logger.warning("%s", message)

    if skip:
        logger.info("--no-gpu: not provisioning GPU scheduling")
        return None

    ctx_helm = _ctx_helm(kube_context)
    ours = helm_release_exists(NVIDIA_PLUGIN_RELEASE, NVIDIA_PLUGIN_NAMESPACE, ctx_helm)
    advertised = get_cluster_allocatable_gpus(kube_context=kube_context)

    # Capacity first, deliberately. A managed GPU node pool (GKE/EKS) or a cluster running
    # the NVIDIA GPU Operator already advertises the resource and has no `nvidia`
    # RuntimeClass, so a RuntimeClass-first check would miss it -- and installing a second
    # advertiser alongside an operator's would silently override its time-slicing config.
    if advertised > 0 and not ours:
        logger.info("%d %s already schedulable (a device plugin or GPU Operator is "
                    "installed by something other than RoboVAST); leaving it alone",
                    advertised, GPU_RESOURCE)
        if requested and gpu_replicas != advertised:
            raise RuntimeError(
                f"--gpu-replicas {gpu_replicas} was requested, but this cluster already "
                f"advertises {advertised} {GPU_RESOURCE} through a device plugin RoboVAST "
                f"does not manage. Change it there, or omit --gpu-replicas to use what is "
                f"already advertised.")
        return advertised

    if not ours and not nvidia_runtime_class_present(kube_context=kube_context):
        return _giveup(
            f"No GPU support detected on this cluster: no node advertises {GPU_RESOURCE} "
            f"and there is no '{NVIDIA_RUNTIME_CLASS}' RuntimeClass (which RKE2 registers "
            f"when the NVIDIA container toolkit is installed on a node). Campaigns will "
            f"render in software, exactly as before.\n"
            f"  Install the NVIDIA driver and container toolkit on the GPU node, or pass "
            f"--no-gpu (or omit --gpu-replicas) to set the cluster up without GPUs.")

    if requested:
        replicas = int(gpu_replicas)
    elif ours:
        # Preserve a deliberate choice across a bare re-run rather than resetting it.
        replicas = _deployed_replicas(kube_context=kube_context) or DEFAULT_GPU_REPLICAS
    else:
        replicas = DEFAULT_GPU_REPLICAS

    if replicas > DEFAULT_GPU_REPLICAS:
        logger.warning(
            "GPU time-slicing set to %d replicas per physical GPU. This caps concurrency; "
            "it does NOT partition VRAM -- all %d renderers allocate from the same card, "
            "so they must fit in its memory or a trial will fail mid-run.",
            replicas, replicas)

    verb = "Upgrading" if ours else "Installing"
    logger.info("%s the NVIDIA device plugin (%d time-slicing replicas per GPU)...",
                verb, replicas)
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", prefix="nvdp_values_", delete=False) as vf:
        vf.write(format_plugin_values(replicas))
        values_path = vf.name
    try:
        _run_helm([
            "upgrade", "--install", NVIDIA_PLUGIN_RELEASE, NVIDIA_PLUGIN_CHART,
            f"--repo={NVIDIA_PLUGIN_REPO}",
            f"--version={NVIDIA_PLUGIN_VERSION}",
            f"--namespace={NVIDIA_PLUGIN_NAMESPACE}",
            "--create-namespace",
            f"--values={values_path}",
        ] + ctx_helm)
    except Exception as exc:  # noqa: BLE001 - policy above decides whether this is fatal
        return _giveup(f"Could not install the NVIDIA device plugin: {exc}. Campaigns "
                       f"will render in software.")
    finally:
        os.unlink(values_path)

    try:
        return _wait_for_gpu_capacity(replicas, kube_context=kube_context)
    except RuntimeError as exc:
        return _giveup(str(exc))


def uninstall_nvidia_device_plugin(kube_context=None):
    """Remove the device plugin, tolerating its absence.

    Never touches the ``nvidia`` RuntimeClass or the host's container toolkit: RKE2 owns
    the first and the node's administrator the second, and a cleanup that removed either
    would break every other workload on the node.
    """
    ok, err = _run_helm(
        ["uninstall", NVIDIA_PLUGIN_RELEASE, f"--namespace={NVIDIA_PLUGIN_NAMESPACE}"]
        + _ctx_helm(kube_context), check=False)
    if not ok and "not found" not in (err or "").lower():
        logger.warning("Could not uninstall the NVIDIA device plugin: %s", err)
