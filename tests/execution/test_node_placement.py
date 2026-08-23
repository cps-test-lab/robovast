# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which node holds RoboVAST's own node-local state.

The bug these pin: a `cleanup` + `setup` left no trace of the previous placement, so the
scheduler chose again, the service came up on a different node with an empty registry, and
setup reported success. Every test here is about the decision being *sticky* -- free space
decides only the first placement, and everything after it defends what already exists.
"""

import json

import pytest

from robovast.execution.cluster_execution import node_placement as np

LABEL = np.DATA_NODE_LABEL


class _Node:
    def __init__(self, name, labels=None, taints=None, ready=True, cordoned=False,
                 ephemeral=None):
        self.metadata = type("M", (), {"name": name, "labels": dict(labels or {})})()
        self.spec = type("S", (), {"taints": list(taints or []),
                                   "unschedulable": cordoned})()
        conditions = [type("C", (), {"type": "Ready",
                                     "status": "True" if ready else "False"})()]
        self.status = type("St", (), {
            "conditions": conditions,
            "allocatable": {"ephemeral-storage": ephemeral} if ephemeral else {}})()


class _Taint:
    def __init__(self, key, value=None, effect="NoSchedule"):
        self.key, self.value, self.effect = key, value, effect


class _Core:
    """The slice of CoreV1Api the resolver uses, with patches recorded."""

    def __init__(self, *nodes, summaries=None):
        self.nodes = list(nodes)
        self.summaries = summaries or {}
        self.patches = []

    def list_node(self, label_selector=None):
        items = self.nodes
        if label_selector:
            wanted = dict(p.split("=", 1) for p in label_selector.split(","))
            items = [n for n in items
                     if all(n.metadata.labels.get(k) == v for k, v in wanted.items())]
        return type("L", (), {"items": items})()

    def patch_node(self, name, body):
        labels = body["metadata"]["labels"]
        node = next(n for n in self.nodes if n.metadata.name == name)
        for key, value in labels.items():
            if value is None:
                node.metadata.labels.pop(key, None)
            else:
                node.metadata.labels[key] = value
        self.patches.append((name, labels))

    def summary_of(self, name):
        if name not in self.summaries:
            raise RuntimeError("nodes/proxy forbidden")
        return {"node": {"fs": {"usedBytes": 0, "availableBytes": self.summaries[name]}}}

    def connect_get_node_proxy_with_path(self, name, path, **kwargs):
        """The real endpoint, raw-bytes shaped, so the production read path is exercised.

        `read_node_summary` asks for `_preload_content=False` and then parses and releases
        the response itself; a fake that returned a dict would skip exactly the handling
        that once handed back a Python repr json.loads could not read.
        """
        assert path == "stats/summary" and kwargs.get("_preload_content") is False
        if name not in self.summaries:
            raise RuntimeError("nodes/proxy forbidden")
        payload = json.dumps(self.summary_of(name)).encode()
        return type("R", (), {"data": payload, "release_conn": lambda self: None})()


def _resolve(core, **kwargs):
    kwargs.setdefault("allow_auto_pick", True)
    return np.resolve_placement(core, LABEL, **kwargs)


# --- the decision is sticky -------------------------------------------------------

def test_an_existing_label_wins_over_free_space():
    """The regression. A re-setup must return to where the data already is.

    node-b has the emptier disk, so an auto-pick would move the deployment onto it and
    leave the registry blobs stranded on node-a. The label is what stops that.
    """
    core = _Core(_Node("node-a", labels={LABEL: "true"}), _Node("node-b"),
                 summaries={"node-a": 10 * 10**9, "node-b": 900 * 10**9})
    placement = _resolve(core)
    assert placement.node == "node-a"
    assert placement.source == "label"
    assert core.patches == []           # nothing re-labelled, nothing moved


def test_the_selector_is_a_constant_never_a_hostname():
    """What makes `upgrade` unable to drop the pin: there is no value to thread."""
    core = _Core(_Node("node-a", labels={LABEL: "true"}))
    assert _resolve(core).selector == {LABEL: "true"}
    assert "kubernetes.io/hostname" not in _resolve(core).selector


def test_upgrade_may_not_pick():
    """`allow_auto_pick=False` is what keeps an upgrade from relocating the data.

    With no label there is no safe answer, and inventing one is how an upgrade silently
    became a move. Deciding placement is `setup`'s job.
    """
    core = _Core(_Node("node-a", ephemeral="900G"), _Node("node-b", ephemeral="10G"))
    assert _resolve(core, allow_auto_pick=False) is None


# --- moving data is an error, not a default ---------------------------------------

def test_requesting_another_node_refuses_while_data_exists():
    core = _Core(_Node("node-a", labels={LABEL: "true"}), _Node("node-b"),
                 summaries={"node-a": 1, "node-b": 2})
    with pytest.raises(np.PlacementConflict) as excinfo:
        _resolve(core, requested="node-b")
    message = str(excinfo.value)
    assert "node-a" in message and "not migrated" in message.lower()
    assert core.patches == []


def test_the_move_is_possible_when_asked_for_explicitly():
    core = _Core(_Node("node-a", labels={LABEL: "true"}), _Node("node-b"),
                 summaries={"node-a": 1, "node-b": 2})
    placement = _resolve(core, requested="node-b", allow_move=True)
    assert placement.node == "node-b"
    assert core.nodes[0].metadata.labels == {}      # the old label is taken off


def test_two_labelled_nodes_is_ambiguous_not_a_coin_flip():
    """Either node matches the selector, so the data would split. Refuse."""
    core = _Core(_Node("node-a", labels={LABEL: "true"}),
                 _Node("node-b", labels={LABEL: "true"}))
    with pytest.raises(np.PlacementConflict):
        _resolve(core)


def test_labelling_a_node_unlabels_every_other():
    """Otherwise the selector matches two nodes and the pod floats between them."""
    core = _Core(_Node("node-a", labels={LABEL: "true"}), _Node("node-b"))
    np.ensure_labeled(core, "node-b", LABEL)
    assert core.nodes[0].metadata.labels == {}
    assert core.nodes[1].metadata.labels == {LABEL: "true"}


# --- auto-pick: schedulable first, then biggest ------------------------------------

def test_the_first_pick_takes_the_emptiest_disk():
    core = _Core(_Node("node-a"), _Node("node-b"),
                 summaries={"node-a": 100 * 10**9, "node-b": 400 * 10**9})
    placement = _resolve(core)
    assert (placement.node, placement.source, placement.signal) == (
        "node-b", "auto", "kubelet-nodefs")


def test_a_tainted_node_is_never_picked_however_big():
    """The trap in ranking by size alone: on a stock RKE2 the biggest disk is often the
    control-plane's, and a pod with no tolerations pinned there stays Pending forever
    while setup reports the placement as chosen."""
    core = _Core(
        _Node("node-a", taints=[_Taint("node-role.kubernetes.io/control-plane")]),
        _Node("node-b"),
        summaries={"node-a": 900 * 10**9, "node-b": 10 * 10**9})
    assert _resolve(core).node == "node-b"


def test_a_tolerated_taint_keeps_the_node_eligible():
    core = _Core(_Node("node-a", taints=[_Taint("dedicated", "batch")]), _Node("node-b"),
                 summaries={"node-a": 900 * 10**9, "node-b": 10 * 10**9})
    tolerations = ({"key": "dedicated", "value": "batch", "effect": "NoSchedule"},)
    assert _resolve(core, tolerations=tolerations).node == "node-a"


@pytest.mark.parametrize("kwargs", [{"cordoned": True}, {"ready": False}])
def test_a_cordoned_or_unready_node_is_not_eligible(kwargs):
    core = _Core(_Node("node-a", **kwargs), _Node("node-b"),
                 summaries={"node-a": 900 * 10**9, "node-b": 10 * 10**9})
    assert _resolve(core).node == "node-b"


def test_no_eligible_node_is_an_error_not_an_arbitrary_pick():
    core = _Core(_Node("node-a", cordoned=True), summaries={"node-a": 1})
    with pytest.raises(np.PlacementConflict):
        _resolve(core)


def test_the_operators_node_pool_narrows_the_candidates():
    """`control.node_labels` constrains where a pin may land, rather than being replaced
    by it -- a pool selector alone still lets the pod float within the pool."""
    core = _Core(_Node("node-a", labels={"pool": "extra"}), _Node("node-b"),
                 summaries={"node-a": 10 * 10**9, "node-b": 900 * 10**9})
    assert _resolve(core, extra_labels={"pool": "extra"}).node == "node-a"


def test_ties_break_on_the_name_so_two_runs_agree():
    core = _Core(_Node("node-b"), _Node("node-a"),
                 summaries={"node-a": 500 * 10**9, "node-b": 500 * 10**9})
    assert _resolve(core).node == "node-a"


# --- signals ----------------------------------------------------------------------

def test_an_unreadable_kubelet_falls_back_and_says_so():
    """A restricted kubeconfig has no nodes/proxy. Ranking still happens, on a different
    measure -- and the reported signal is what tells the reader the numbers changed
    meaning from free space to capacity."""
    core = _Core(_Node("node-a", ephemeral="900G"), _Node("node-b", ephemeral="10G"))
    placement = _resolve(core)
    assert (placement.node, placement.signal) == ("node-a", "allocatable")


def test_ranking_mixes_both_signals_without_crashing():
    core = _Core(_Node("node-a", ephemeral="900G"), _Node("node-b", ephemeral="10G"),
                 summaries={"node-b": 50 * 10**9})
    ranked = np.rank_by_free_space(core, ["node-a", "node-b"],
                                   read_summary=core.summary_of)
    assert [r[2] for r in ranked] == ["allocatable", "kubelet-nodefs"]
    assert ranked[0][0] == "node-a"


@pytest.mark.parametrize("value,expected", [
    ("900G", 900 * 1000**3), ("10Gi", 10 * 1024**3), ("1024", 1024),
    (None, 0), ("nonsense", 0)])
def test_storage_quantities_parse_or_sort_last(value, expected):
    assert np._parse_quantity(value) == expected


# --- storage that is not node-local ------------------------------------------------

def test_a_provisioned_volume_is_not_pinned():
    """A nodeSelector on a CSI-backed pod is noise at best; with a zonal disk it is
    unschedulable."""
    core = _Core(_Node("node-a"), summaries={"node-a": 1})
    assert _resolve(core, node_local=False) is None
    assert core.patches == []


def test_an_explicit_flag_is_reported_rather_than_dropped(caplog):
    """Silently ignoring a flag the operator typed is how a deployment ends up somewhere
    nobody chose."""
    core = _Core(_Node("node-a"), summaries={"node-a": 1})
    with caplog.at_level("INFO"):
        assert _resolve(core, node_local=False, requested="node-a") is None
    assert "not node-local" in caplog.text


# --- forgetting is deliberate ------------------------------------------------------

def test_clearing_labels_is_explicit_and_reports_what_it_cleared():
    core = _Core(_Node("node-a", labels={LABEL: "true"}),
                 _Node("node-b", labels={np.BUILD_NODE_LABEL: "true"}))
    assert np.clear_labels(core) == ["node-a", "node-b"]
    assert core.nodes[0].metadata.labels == {}
    assert core.nodes[1].metadata.labels == {}
