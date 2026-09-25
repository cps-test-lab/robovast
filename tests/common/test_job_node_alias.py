# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``execution.kubernetes.jobs.node`` names a registered alias, never a machine or a selector.

A campaign that confines its jobs to one node names an alias the operator registered, so the
file carries no node name. Every shape that is not an alias is refused with its own remedy,
and the cluster-wide pool keys are refused by name rather than by a generic schema error.
"""

import logging

import pytest

from robovast.common.config import validate_config
from robovast.common.execution import job_node_alias


def _cfg(kubernetes=None, **execution):
    ex = {"containers": {"scenario": {"image": "a"}}, "runs": 1, **execution}
    if kubernetes is not None:
        ex["kubernetes"] = kubernetes
    return {"version": 6, "execution": ex, "configuration": [{"name": "a"}]}


def _node(value):
    return _cfg({"jobs": {"node": value}})


# -- the alias --------------------------------------------------------------------

@pytest.mark.parametrize("alias", ["bench-a", "a", "node_1", "0", "a" * 63])
def test_an_alias_is_accepted(alias):
    assert validate_config(_node(alias)).execution.kubernetes.jobs.node == alias


def test_the_block_is_optional():
    assert validate_config(_cfg()).execution.kubernetes is None
    assert validate_config(_cfg({})).execution.kubernetes.jobs is None
    assert validate_config(_cfg({"jobs": {}})).execution.kubernetes.jobs.node is None


@pytest.mark.parametrize("value, says", [
    ("", "is empty"),
    ("role=bench", "label selector"),
    ("robovast.io/job-node-alias", "label key"),
    ("worker.example.com", "looks like a node name"),
    ("Bench", "looks like a node name"),
    ("a" * 64, "at most 63"),
    ("-bench", "not a valid alias"),
    ("bench_", "not a valid alias"),
    ("be nch", "not a valid alias"),
])
def test_each_non_alias_shape_has_its_own_message(value, says):
    with pytest.raises(ValueError, match=says):
        validate_config(_node(value))


@pytest.mark.parametrize("value", ["", "role=bench", "a/b", "Host.example.com"])
def test_every_remedy_names_where_an_alias_is_registered_where_one_applies(value):
    with pytest.raises(ValueError) as exc:
        validate_config(_node(value))
    assert "ROBOVAST_JOB_NODE_ALIASES" in str(exc.value)


def test_an_unknown_key_in_the_block_is_refused():
    with pytest.raises(ValueError, match="kubernetes.jobs.nodes"):
        validate_config(_cfg({"jobs": {"nodes": "bench-a"}}))


# -- the pool keys are cluster settings --------------------------------------------

def test_jobs_node_labels_names_the_setup_option_and_the_alias():
    with pytest.raises(ValueError) as exc:
        validate_config(_cfg({"jobs": {"node_labels": {"pool": "a"}}}))
    text = str(exc.value)
    assert "ROBOVAST_JOB_NODE_LABELS" in text
    assert "execution.kubernetes.jobs.node" in text
    assert "Extra inputs are not permitted" not in text


def test_control_names_the_setup_option():
    with pytest.raises(ValueError) as exc:
        validate_config(_cfg({"control": {"node_labels": {"pool": "b"}}}))
    assert "--control-node-label KEY=VALUE" in str(exc.value)
    assert "Extra inputs are not permitted" not in str(exc.value)


# -- archive reads -----------------------------------------------------------------

def test_an_archived_campaign_with_the_pool_keys_still_reads(caplog):
    """The keys never reached a run, so reading the campaign back drops them."""
    with caplog.at_level(logging.WARNING):
        c = validate_config(_cfg({"jobs": {"node_labels": {"pool": "a"}},
                                  "control": {"node_labels": {"pool": "b"}}}), strict=False)
    assert c.execution.kubernetes is None
    assert "execution.kubernetes" in caplog.text


def test_a_lenient_read_keeps_a_valid_alias():
    """A pinned campaign read back, retriggered or seeded must stay pinned."""
    c = validate_config(_node("bench-a"), strict=False)
    assert c.execution.kubernetes.jobs.node == "bench-a"


def test_a_lenient_read_keeps_the_alias_and_drops_only_the_pool_keys():
    c = validate_config(_cfg({"jobs": {"node": "bench-a", "node_labels": {"pool": "a"}},
                              "control": {}}), strict=False)
    assert c.execution.kubernetes.jobs.node == "bench-a"


def test_a_lenient_read_still_refuses_a_bad_alias():
    """A pin is what the run did, so an unreadable one is an error, not something to drop."""
    with pytest.raises(ValueError, match="looks like a node name"):
        validate_config(_node("worker.example.com"), strict=False)


def test_a_strict_read_does_not_drop_anything():
    with pytest.raises(ValueError, match="ROBOVAST_JOB_NODE_LABELS"):
        validate_config(_cfg({"jobs": {"node_labels": {"pool": "a"}}}))


# -- the reader --------------------------------------------------------------------

def test_the_reader_reads_a_raw_mapping():
    assert job_node_alias(_node("bench-a")) == "bench-a"


def test_the_reader_reads_a_validated_model():
    assert job_node_alias(validate_config(_node("bench-a"))) == "bench-a"


def test_the_reader_reads_a_mapping_holding_a_validated_execution():
    model = validate_config(_node("bench-a"))
    assert job_node_alias({"execution": model.execution}) == "bench-a"


@pytest.mark.parametrize("data", [
    {}, {"execution": None}, _cfg(), _cfg({}), _cfg({"jobs": None}), _cfg({"jobs": {}}),
])
def test_the_reader_answers_none_when_unset(data):
    assert job_node_alias(data) is None
    if data.get("execution"):
        assert job_node_alias(validate_config(data)) is None
