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

"""Where RoboVAST's own node-local state lives, decided once and made durable.

Everything a deployment keeps is node-local by default: the object store and the campaign
index beside it, the workspaces and the results, the registry, and the build cache -- a
``hostPath`` each. A stock cluster ships no StorageClass, so ``hostPath`` is not a preference
here, it is the fallback that works. The pin therefore holds the campaigns themselves, not
only rebuildable blobs: a deployment that came up on another node would find an empty store.

Nothing pinned them, and the failure that follows is silent in both directions. A
``cleanup`` followed by a ``setup`` left no trace of the previous placement, so the
scheduler was free to choose again and on a heterogeneous cluster it did: the service came
up on a different node with an **empty registry** -- the blobs still on the old node's disk,
intact and unreachable -- while setup reported success. The ``disk`` meter moved at the same
moment, because it reports the filesystem of the node carrying the service pod, and that is
a different machine now.

The fix is a **node label**, not a hostname threaded through the call graph:

* It is what Kubernetes actually schedules on, so the record and the mechanism are one
  object and cannot disagree.
* It is cluster-scoped, so it **survives ``cluster cleanup``** -- precisely the moment a
  placement recorded anywhere else is forgotten. A later ``setup`` with no flags at all
  lands where the previous one did.
* The manifests then carry a **constant** selector. A caller cannot forget to pass it,
  which is how ``upgrade`` silently unpins the service pod: it calls the service deploy
  without the node argument, and "not passed" reads as "unpinned".
* No hostname appears in any manifest, which the threaded form could not avoid.

Stickiness beats cleverness. Free space decides only the **first** placement on a cluster;
after that an existing label wins. Naming a node explicitly is the one thing that overrides
it, in one flag -- typing a node name is already the deliberate act, so a second confirming
flag only stood between the operator and what they had asked for. The move is not silent:
the node the label came off is reported, because the bytes stay there and nothing else in
the deployment will ever mention them again. See :func:`resolve_placement`.
"""

import logging
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

#: The taint a campaign node may carry, and so what anything running *where campaigns run*
#: must tolerate. The taint is a property of the cluster's nodes and outlives whatever admits
#: the jobs, which is why it lives here. ``image_warm`` reads it so its DaemonSet cannot drift
#: from the job pods and skip precisely the nodes worth warming.
#:
#: **A job pod must carry this itself.** Nothing else injects it, and a deployment that taints
#: its campaign nodes without it does not fail loudly -- its pods simply never place.
CAMPAIGN_NODE_TOLERATIONS = ({"key": "dedicated", "value": "batch", "effect": "NoSchedule"},)

#: The node holding the service pod (workspaces + registry) and, where it is node-local,
#: the results store. One label for both because they are one decision: the disk meter
#: reports the service's node, so splitting them would make the meter answer about a node
#: that holds none of the data it is being read to reason about.
#: This node's identity, as a **schedulable** selector.
#:
#: The value is :func:`~robovast.execution.data.collect_sysinfo.node_label` of the node's
#: name -- the same sha256 prefix ``runs.node_label`` already records -- so the selector that
#: placed a run and the sysinfo that describes the machine it ran on are provably the same
#: node, with no mapping table to keep in step and no hostname in any manifest.
#:
#: ``kubernetes.io/hostname`` would already be a schedulable selector and is deliberately not
#: used: keeping node names out of every sink is the point of the hash, and a nodeSelector is
#: a sink -- it is recorded in the pod spec, which travels with the campaign.
#:
#: Unlike :data:`DATA_NODE_LABEL` this is **not exclusive**: every node carries its own
#: distinct value, because it answers "which node is this" rather than "which node was
#: chosen".
NODE_ID_LABEL = "robovast.io/node-id"

#: The node pool campaign jobs may run on, as ``{label: value}`` -- what
#: ``execution.kubernetes.jobs.node_labels`` means now.
#:
#: It reaches the running service through this env var rather than through a ``.vast``,
#: because it is a property of the CLUSTER and not of a campaign: a per-campaign override
#: would let one campaign widen the pool every other one is confined to. ``setup_server``
#: reads the operator's file and stamps it here, which is the same path the headroom figures
#: take.
#:
#: Two consumers, and both are needed for it to mean anything. The budget provider counts
#: only matching nodes, so nothing outside the pool is ever offered as capacity; and every
#: job pod carries the same labels as a real ``nodeSelector``, so the scheduler is bound by
#: the same rule the accounting assumed. Filtering capacity alone would leave kube-scheduler
#: free to place outside the pool; stamping alone would have admission promising room on
#: nodes the pods may not use.
JOB_NODE_POOL_ENV = "ROBOVAST_JOB_NODE_LABELS"


def job_node_pool() -> dict:
    """The configured pool, or ``{}`` for "every node" -- see :data:`JOB_NODE_POOL_ENV`.

    Raises rather than falling back on a value it cannot parse: a typo that silently became
    "every node" would scatter a campaign across machines the operator had excluded, and the
    symptom appears nowhere near the cause.
    """
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    raw = (os.environ.get(JOB_NODE_POOL_ENV) or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{JOB_NODE_POOL_ENV}={raw!r} is not JSON: {exc}") from exc
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError(f"{JOB_NODE_POOL_ENV}={raw!r} must be a JSON object of "
                         "label -> value strings")
    return value

DATA_NODE_LABEL = "robovast.io/data-node"

#: The node holding the shared build daemon's cache. Its own label because the cache is the
#: deployment's other large on-disk tenant and a big cluster may want it elsewhere -- but it
#: defaults to the data node (see :func:`resolve_placement`), because auto-separating puts a
#: 150 GB cache on whatever node was left over, which is usually the smaller one.
BUILD_NODE_LABEL = "robovast.io/build-node"

#: ``"true"``, not ``""``. An empty value is legal in Kubernetes but a hand-typed
#: ``kubectl label node n robovast.io/data-node=`` then produces a label that a
#: ``nodeSelector`` of ``{...: "true"}`` does not match, with nothing to see in
#: ``kubectl get nodes -L`` to explain why. A value that is visibly present or visibly
#: absent has no such middle state.
LABEL_VALUE = "true"

#: Taints a workload tolerates by virtue of being infrastructure rather than a campaign run.
#: Empty: the service pod carries no tolerations, so a tainted node is simply not eligible
#: for it. Callers with tolerations of their own (the build daemon) pass them in.
_NO_TOLERATIONS = ()


class PlacementConflict(RuntimeError):
    """No placement can be resolved: the named node cannot host the role, no node can, or
    two nodes carry the label and the pod would float between them.

    Raised rather than warned, because the outcome it prevents is invisible: the service
    comes up healthy with an empty registry, and the first symptom is a campaign that
    cannot pull an image it was built with, hours later and somewhere else.
    """


class Placement(NamedTuple):
    """A resolved placement.

    ``node`` is the node to label; ``selector`` is what goes into the pod spec -- always
    the constant label, never ``kubernetes.io/hostname``. ``source`` says which rule in
    :func:`resolve_placement` fired (``requested`` / ``label`` / ``auto``)
    and ``signal`` names the measurement an ``auto`` pick used, so the setup log can state
    on what evidence it chose.

    ``previous`` names the node the label was taken off, when this placement moved it, and
    is ``None`` otherwise. The caller prints it: the data stays on that node, and once the
    label is gone nothing in the deployment refers to it again.
    """

    node: str
    selector: dict
    source: str
    signal: Optional[str] = None
    previous: Optional[str] = None


def label_selector(label: str) -> dict:
    """The ``nodeSelector`` for *label* -- a constant, independent of which node it is on."""
    return {label: LABEL_VALUE}


def labeled_nodes(core, label: str) -> list:
    """Nodes carrying *label*, by name, sorted.

    Server-side ``label_selector`` rather than a client-side filter over every node: on a
    large cluster this is the difference between one small response and the whole node list
    on a path that runs during setup and again on every upgrade.
    """
    items = core.list_node(label_selector=f"{label}={LABEL_VALUE}").items
    return sorted(n.metadata.name for n in items)


def _tolerates(taint, tolerations) -> bool:
    """Whether *tolerations* covers one taint, by the Kubernetes matching rules."""
    effect = getattr(taint, "effect", None)
    if effect not in ("NoSchedule", "NoExecute"):
        return True     # PreferNoSchedule does not make a node ineligible
    for tol in tolerations or ():
        tol_effect = tol.get("effect")
        if tol_effect and tol_effect != effect:
            continue
        operator = tol.get("operator", "Equal")
        if operator == "Exists":
            if not tol.get("key") or tol.get("key") == getattr(taint, "key", None):
                return True
        elif (tol.get("key") == getattr(taint, "key", None)
              and tol.get("value") == getattr(taint, "value", None)):
            return True
    return False


def node_is_schedulable(node, tolerations=_NO_TOLERATIONS) -> bool:
    """Whether a pod carrying *tolerations* could actually be placed on this node object.

    **One predicate, because two answers to "is this node usable" is one answer too many.**
    Placement asked it here while the budget provider did not ask it at all, and the
    disagreement had a cost: a node that dies mid-campaign loses its pods after the eviction
    timeout and then reads as *fully free*, so admission keeps reserving room on a machine
    Kubernetes will not schedule to. The pods report an untolerated ``not-ready`` taint, which
    is correctly classified as a fault rather than contention, so they are dropped on the
    short grace window -- and the next drain does it again, discarding runs for as long as the
    node is down. A cordon for maintenance produces the same loop.

    The three tests are the ones the scheduler itself applies before anything else: cordoned,
    not ``Ready``, or carrying a taint this workload does not tolerate.
    """
    if getattr(node.spec, "unschedulable", False):
        return False
    conditions = {c.type: c.status for c in (node.status.conditions or [])}
    if conditions.get("Ready") != "True":
        return False
    return not any(not _tolerates(t, tolerations) for t in (node.spec.taints or []))


def eligible_nodes(core, tolerations=_NO_TOLERATIONS, extra_labels=None) -> list:
    """Nodes a workload could actually be scheduled onto, by name, sorted.

    Ranking by free space without this filter is the trap: on a stock RKE2 the biggest disk
    is often the control-plane's, and it carries
    ``node-role.kubernetes.io/control-plane:NoSchedule``. Labelling it would report a
    placement as chosen and then leave the pod ``Pending`` forever -- a setup that succeeds
    and a cluster that never runs anything.

    *extra_labels* narrows the candidates further (the operator's own
    ``control.node_labels`` pool), so a pin is always chosen from within a pool the operator
    allowed rather than beside it.
    """
    selector = ",".join(f"{k}={v}" for k, v in (extra_labels or {}).items()) or None
    return sorted(node.metadata.name
                  for node in core.list_node(label_selector=selector).items
                  if node_is_schedulable(node, tolerations))


def rank_by_free_space(core, names, read_summary=None, timeout_s: float = 2.0) -> list:
    """*names* ordered by free bytes, most first: ``[(name, free_bytes, signal), ...]``.

    Two signals, and the caller is told which was used rather than being handed a number
    whose provenance it cannot see. The kubelet's ``stats/summary`` is the real free space
    on the filesystem the data will land on. It needs the ``nodes/proxy`` subresource,
    which an operator's kubeconfig has and a restricted one may not; when a node's read
    fails, its ``allocatable["ephemeral-storage"]`` stands in. That is a capacity, not a
    free figure, so it ranks the same nodes differently -- which is exactly why it is
    reported and not silently substituted.

    Ties break on the node name so a cluster of identical nodes resolves the same way twice.
    """
    from .kube_client import (  # pylint: disable=import-outside-toplevel
        read_node_summary, nodefs_used_available)
    read_summary = read_summary or (lambda n: read_node_summary(core, n, timeout_s))
    allocatable = {}
    for node in core.list_node().items:
        allocatable[node.metadata.name] = (node.status.allocatable or {}).get(
            "ephemeral-storage")
    ranked = []
    for name in names:
        try:
            _, available = nodefs_used_available(read_summary(name))
            if available is not None:
                ranked.append((name, available, "kubelet-nodefs"))
                continue
            reason = "the kubelet Summary carried no node filesystem"
        except Exception as e:      # noqa: BLE001 - one unreadable node must not end the pick
            reason = f"{e.__class__.__name__}: {e}"
        # The node is named in the log, never in anything returned: these strings reach a
        # CLI the operator reads, but the same helpers feed values that cross to a UI.
        logger.debug("kubelet Summary unavailable on node %s (%s); "
                     "ranking it by allocatable ephemeral-storage", name, reason)
        ranked.append((name, _parse_quantity(allocatable.get(name)), "allocatable"))
    return sorted(ranked, key=lambda r: (-r[1], r[0]))


def _parse_quantity(value) -> int:
    """A Kubernetes storage quantity as bytes; ``0`` when absent or unparseable.

    ``0`` rather than a raise: this feeds a ranking, and a node whose size cannot be read
    should sort last, not abort the placement of every other node.
    """
    if not value:
        return 0
    text = str(value).strip()
    suffixes = (("Ei", 1024 ** 6), ("Pi", 1024 ** 5), ("Ti", 1024 ** 4), ("Gi", 1024 ** 3),
                ("Mi", 1024 ** 2), ("Ki", 1024), ("E", 1000 ** 6), ("P", 1000 ** 5),
                ("T", 1000 ** 4), ("G", 1000 ** 3), ("M", 1000 ** 2), ("k", 1000))
    for suffix, factor in suffixes:
        if text.endswith(suffix):
            try:
                return int(float(text[:-len(suffix)]) * factor)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def ensure_labeled(core, node: str, label: str, dry_run: bool = False) -> list:
    """Put *label* on *node*, take it off every other node, and return those others.

    Both halves matter. Adding without removing leaves two labelled nodes, and a
    ``nodeSelector`` then matches either -- the float this module exists to stop, only
    harder to see because the labels say the decision was made.

    The unlabelled nodes are returned rather than only logged: they are where the data
    still is, and that is the one fact the caller has to be able to put on screen.
    """
    removed = []
    for other in labeled_nodes(core, label):
        if other != node:
            logger.info("removing %s from node %s", label, other)
            if not dry_run:
                core.patch_node(other, {"metadata": {"labels": {label: None}}})
            removed.append(other)
    if not dry_run:
        core.patch_node(node, {"metadata": {"labels": {label: LABEL_VALUE}}})
    logger.info("node %s labelled %s=%s", node, label, LABEL_VALUE)
    return removed


def ensure_node_id_labels(core, dry_run: bool = False) -> dict:
    """Give every node its identity label; return ``{node_name: value}`` for those changed.

    Idempotent by value, so a re-run of ``setup`` on an unchanged cluster patches nothing --
    which is what makes it safe to call on every setup rather than only the first.

    A node that joins later is simply unlabelled until the next setup. Callers must treat a
    missing label as "cannot pin here" rather than as an error: refusing to admit anything to
    a new node would turn adding capacity into an outage, and the pin is an optimisation
    where the node identity is an accounting fact.
    """
    from robovast.execution.data.collect_sysinfo import node_label  # noqa: PLC0415

    changed = {}
    for node in core.list_node().items:
        name = node.metadata.name
        want = node_label(name)
        if not want or (node.metadata.labels or {}).get(NODE_ID_LABEL) == want:
            continue
        logger.info("labelling node %s with its identity", name)
        if not dry_run:
            core.patch_node(name, {"metadata": {"labels": {NODE_ID_LABEL: want}}})
        changed[name] = want
    return changed


def apply_node_id_labels(kube_context=None, dry_run: bool = False) -> dict:
    """Load the kube config, then :func:`ensure_node_id_labels`. Returns what changed.

    The client-building wrapper exists so ``setup`` calls ONE name, the same shape as
    ``ensure_nvidia_device_plugin`` -- which is also what makes it stubbable. A version that
    built ``CoreV1Api()`` inside ``setup_server`` dialled a real API server from every test
    that stubs the rest of setup, turning a 40-second suite into eleven minutes of connection
    timeouts.
    """
    from kubernetes import client  # noqa: PLC0415

    from .kube_client import load_kube_config  # noqa: PLC0415

    load_kube_config(context=kube_context)
    return ensure_node_id_labels(client.CoreV1Api(), dry_run=dry_run)


def clear_labels(core, labels=(DATA_NODE_LABEL, BUILD_NODE_LABEL)) -> list:
    """Remove *labels* from every node; returns the nodes that carried one.

    Only ``cleanup --forget-placement`` calls this. Forgetting is deliberately not the
    default: the labels are the stickiness, and clearing them on every teardown would
    restore exactly the behaviour this module removes.
    """
    cleared = []
    for label in labels:
        for node in labeled_nodes(core, label):
            core.patch_node(node, {"metadata": {"labels": {label: None}}})
            cleared.append(node)
    return sorted(set(cleared))


def resolve_placement(core, label: str, *, node_local: bool = True, requested: str = "",
                      allow_auto_pick: bool = True,
                      tolerations=_NO_TOLERATIONS, extra_labels=None,
                      dry_run: bool = False):
    """Decide which node holds this role, label it, and return the :class:`Placement`.

    ``None`` means "do not pin", which is a real answer and not a failure: with a
    StorageClass or a cloud bucket behind the volume the data is not on a node at all, and
    a ``nodeSelector`` there is at best noise and at worst unschedulable (a zonal disk and
    the wrong zone).

    The order is stickiness-first, and every step before the last one is about *not*
    moving data that already exists:

    0. ``node_local`` is false -- nothing to pin. An explicit *requested* is still reported
       rather than dropped, because silently ignoring a flag the operator typed is how a
       deployment ends up somewhere nobody chose.
    1. *requested* -- an explicit ``--data-node``. Must be eligible. It wins outright,
       including over a label already on another node: naming a node *is* the deliberate
       act. The move is not silent -- the abandoned node comes back as
       :attr:`Placement.previous` and its bytes are **not** migrated.
    2. The existing label. Exactly one node -- the ordinary path on every run after the
       first, and the reason a ``cleanup`` + ``setup`` returns to where it was.
    3. Free space, most first, among eligible nodes. Only with *allow_auto_pick*: ``setup``
       may choose, ``upgrade`` may not, because an upgrade that picks is an upgrade that
       can move the data it was asked to leave alone. An upgrade with no label to read
       therefore returns ``None`` and says so -- ``setup`` is what decides placement.
    """
    if not node_local:
        if requested:
            logger.info(
                "--%s was given but this role's storage is not node-local (a "
                "StorageClass or an external bucket backs it), so no node pin is "
                "applied; the volume follows the pod.", label.split("/")[-1])
        return None

    labelled = labeled_nodes(core, label)

    if requested:
        eligible = eligible_nodes(core, tolerations, extra_labels)
        if requested not in eligible:
            raise PlacementConflict(
                f"node {requested!r} cannot host this role: it is not Ready, is cordoned, "
                f"carries a taint the pod does not tolerate, or is outside the configured "
                f"node pool. Eligible: {', '.join(eligible) or '(none)'}")
        moved_from = ", ".join(ensure_labeled(core, requested, label, dry_run))
        if moved_from:
            logger.warning(
                "%s moves from %s to %s. The workspaces, registry blobs and cache already "
                "written stay on %s and are NOT migrated -- the new node starts empty and "
                "rebuilds what it needs.", label, moved_from, requested, moved_from)
        return Placement(requested, label_selector(label), "requested",
                         previous=moved_from or None)

    if len(labelled) == 1:
        logger.info("%s: %s (existing label)", label, labelled[0])
        return Placement(labelled[0], label_selector(label), "label")
    if len(labelled) > 1:
        raise PlacementConflict(
            f"{len(labelled)} nodes carry {label} ({', '.join(labelled)}), so the pod "
            f"could schedule onto either and the data would be split between them. "
            f"Remove the label from all but one, or name the right one with the "
            f"placement flag.")

    if not allow_auto_pick:
        logger.warning(
            "no node carries %s, so this deployment's node-local data is not pinned and "
            "the scheduler may place it anywhere. Run `vast cluster setup` to decide "
            "a placement; an upgrade deliberately does not, because picking here would "
            "move the data it was asked to leave alone.", label)
        return None

    eligible = eligible_nodes(core, tolerations, extra_labels)
    if not eligible:
        raise PlacementConflict(
            "no node can host this deployment's data: every node is cordoned, not Ready, "
            "carries a taint the pod does not tolerate, or is outside the configured node "
            "pool.")
    ranked = rank_by_free_space(core, eligible)
    logger.info("%s: not found on any node -- picking by free space", label)
    for name, free, signal in ranked:
        logger.info("  %-30s %8.1f GB free  (%s)%s", name, free / 1e9, signal,
                    "  <- chosen" if name == ranked[0][0] else "")
    node, _, signal = ranked[0]
    ensure_labeled(core, node, label, dry_run)
    return Placement(node, label_selector(label), "auto", signal)
