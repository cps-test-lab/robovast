# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The node identity label: a schedulable selector that is not a hostname."""

import types
from unittest import mock

from robovast.execution.cluster_execution.node_placement import (NODE_ID_LABEL,
                                                                 ensure_node_id_labels)
from robovast.execution.data.collect_sysinfo import node_label


def _node(name, labels=None):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels=labels or {}))


def _core(nodes):
    core = mock.Mock()
    core.list_node.return_value = types.SimpleNamespace(items=nodes)
    return core


def test_each_node_gets_the_same_hash_the_results_record():
    """The whole point of deriving it rather than inventing one: the selector that placed a
    run and ``runs.node_label`` in that run's own results are provably the same machine, with
    no mapping table to keep in step."""
    core = _core([_node("worker-02"), _node("some-other-node")])
    changed = ensure_node_id_labels(core)

    assert changed == {"worker-02": node_label("worker-02"),
                       "some-other-node": node_label("some-other-node")}
    for name, value in changed.items():
        core.patch_node.assert_any_call(
            name, {"metadata": {"labels": {NODE_ID_LABEL: value}}})


def test_no_node_name_appears_in_the_label_value():
    """Keeping node names out of every sink is why the hash exists, and a nodeSelector IS a
    sink -- it is recorded in the pod spec, which travels with the campaign. Using
    ``kubernetes.io/hostname`` would have been simpler and would have undone that."""
    name = "worker-02"
    value = ensure_node_id_labels(_core([_node(name)]))[name]
    assert name not in value
    assert value.startswith("node-")


def test_a_node_already_correct_is_not_patched():
    """Idempotent BY VALUE, which is what makes it safe to run on every setup rather than
    only the first: an unchanged cluster is not touched at all."""
    name = "worker-02"
    core = _core([_node(name, {NODE_ID_LABEL: node_label(name)})])
    assert ensure_node_id_labels(core) == {}
    core.patch_node.assert_not_called()


def test_a_wrong_value_is_corrected():
    """A node renamed, or relabelled by hand, must converge rather than keep an identity that
    now points at nothing."""
    name = "worker-02"
    core = _core([_node(name, {NODE_ID_LABEL: "node-deadbeefdead"})])
    assert ensure_node_id_labels(core) == {name: node_label(name)}


def test_dry_run_reports_without_patching():
    core = _core([_node("worker-02")])
    assert ensure_node_id_labels(core, dry_run=True)
    core.patch_node.assert_not_called()


def test_the_label_is_not_exclusive_unlike_the_placement_labels():
    """``ensure_labeled`` removes a placement label from every other node, because it answers
    "which node was chosen". This answers "which node is this", so every node keeps its own
    value -- stripping the others would leave exactly one pinnable node."""
    core = _core([_node("a"), _node("b"), _node("c")])
    changed = ensure_node_id_labels(core)
    assert len(changed) == 3
    assert len(set(changed.values())) == 3, "each node needs a distinct identity"
    # Nothing is ever set to None, which is how a label is removed.
    for call in core.patch_node.call_args_list:
        assert None not in call.args[1]["metadata"]["labels"].values()
