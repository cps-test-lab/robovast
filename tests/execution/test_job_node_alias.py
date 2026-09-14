# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The job node alias registry: an operator's name for one machine a campaign may use.

What these pin: the operator states the registry in ``ROBOVAST_JOB_NODE_ALIASES``, and
setup and upgrade both reconcile the node labels to exactly that; an alias narrows the job
node pool and can never leave it, checked before anything is changed and again at campaign
start; each way an alias fails to name a usable node is a distinct cause; and a refusal
that can reach a campaign names the alias, never the node.
"""

import logging
from unittest import mock

import pytest
from click.testing import CliRunner

from robovast.execution.cluster_execution import cli as cluster_cli
from robovast.execution.cluster_execution import cluster_setup
from robovast.execution.cluster_execution import node_placement as np

ALIAS = np.JOB_NODE_ALIAS_LABEL
ENV = np.JOB_NODE_ALIASES_ENV
POOL = {"node-pool": "primary"}


class _Node:
    def __init__(self, name, labels=None, taints=None, ready=True, cordoned=False):
        self.metadata = type("M", (), {"name": name, "labels": dict(labels or {})})()
        self.spec = type("S", (), {"taints": list(taints or []), "unschedulable": cordoned})()
        self.status = type("St", (), {"conditions": [type("C", (), {
            "type": "Ready", "status": "True" if ready else "False"})()]})()


class _Taint:
    def __init__(self, key, value=None, effect="NoSchedule"):
        self.key, self.value, self.effect = key, value, effect


class _Core:
    """CoreV1Api's node list and patch, with equality and existence selectors."""

    def __init__(self, *nodes):
        self.nodes = list(nodes)
        self.patches = []

    def list_node(self, label_selector=None):
        items = self.nodes
        for term in (label_selector or "").split(","):
            if not term:
                continue
            key, sep, value = term.partition("=")
            items = [n for n in items if (n.metadata.labels.get(key) == value if sep
                                          else key in n.metadata.labels)]
        return type("L", (), {"items": items})()

    def patch_node(self, name, body):
        node = next(n for n in self.nodes if n.metadata.name == name)
        for key, value in body["metadata"]["labels"].items():
            if value is None:
                node.metadata.labels.pop(key, None)
            else:
                node.metadata.labels[key] = value
        self.patches.append((name, body["metadata"]["labels"]))


def _pooled(name, **kwargs):
    labels = {**POOL, np.NODE_ID_LABEL: f"id-{name}", **kwargs.pop("labels", {})}
    return _Node(name, labels=labels, **kwargs)


# -- the shape of an alias ---------------------------------------------------------------

@pytest.mark.parametrize("alias", ["bench", "node_1", "a", "big-box-2", "a" * 63])
def test_a_legal_alias(alias):
    assert np.job_node_alias_problem(alias) is None


@pytest.mark.parametrize("alias", ["", "Bench", "a.b", "a/b", "a=b", "-a", "a-", "a b",
                                   "a" * 64, None])
def test_an_alias_that_is_not_a_plain_label_value_is_refused(alias):
    assert np.job_node_alias_problem(alias)


@pytest.mark.parametrize("alias", ["bench", "a.b", "Bench", "a" * 64, "x_y-1"])
def test_the_registry_and_the_schema_hold_one_rule(alias):
    """An alias the operator can register is exactly one a campaign can name."""
    from robovast.common.config import validate_job_node_alias
    try:
        validate_job_node_alias(alias)
        schema_accepts = True
    except ValueError:
        schema_accepts = False
    assert (np.job_node_alias_problem(alias) is None) is schema_accepts


# -- the environment variable ------------------------------------------------------------

def test_unset_or_empty_states_no_aliases():
    assert np.parse_job_node_aliases(None) == {}
    assert np.parse_job_node_aliases("  ") == {}


def test_a_json_object_of_alias_to_node():
    assert np.parse_job_node_aliases('{"bench": "node-a", "gpu": " node-b "}') == {
        "bench": "node-a", "gpu": "node-b"}


@pytest.mark.parametrize("raw,match", [
    pytest.param("bench=node-a", "not JSON", id="not-json"),
    pytest.param('["bench"]', "JSON object", id="not-an-object"),
    pytest.param('{"Bench": "node-a"}', "lowercase", id="illegal-alias"),
    pytest.param('{"bench": ""}', "names no node", id="empty-node"),
    pytest.param('{"bench": 3}', "names no node", id="node-not-a-string"),
    pytest.param('{"bench": "node-a", "bench": "node-b"}', "more than once", id="repeated"),
])
def test_a_malformed_value_is_refused_naming_the_variable(raw, match):
    with pytest.raises(ValueError, match=match) as exc:
        np.parse_job_node_aliases(raw)
    assert ENV in str(exc.value)


def test_every_problem_is_reported_at_once():
    with pytest.raises(ValueError) as exc:
        np.parse_job_node_aliases('{"A": "node-a", "b.c": "node-b", "ok": ""}')
    assert str(exc.value).count("\n") == 3


def test_it_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(ENV, '{"bench": "node-a"}')
    assert np.job_node_aliases_from_env() == {"bench": "node-a"}
    monkeypatch.delenv(ENV)
    assert np.job_node_aliases_from_env() == {}


# -- the registry ------------------------------------------------------------------------

def test_the_registry_groups_nodes_by_alias():
    core = _Core(_Node("node-a", {ALIAS: "bench"}), _Node("node-b", {ALIAS: "bench"}),
                 _Node("node-c", {ALIAS: "gpu"}), _Node("node-d"))
    assert np.registered_aliases(core) == {"bench": ["node-a", "node-b"], "gpu": ["node-c"]}


def test_reconciling_removes_an_alias_that_is_no_longer_named():
    core = _Core(_Node("node-a", {ALIAS: "bench"}), _Node("node-b", {ALIAS: "old"}),
                 _Node("node-c"))
    changes = np.ensure_alias_labels(core, {"bench": "node-a", "gpu": "node-c"})
    assert np.registered_aliases(core) == {"bench": ["node-a"], "gpu": ["node-c"]}
    assert changes.labelled == {"gpu": "node-c"}
    assert changes.unlabelled == {"old": ["node-b"]}
    assert changes.registry == {"bench": "node-a", "gpu": "node-c"}


def test_reconciling_to_nothing_clears_the_registry():
    core = _Core(_Node("node-a", {ALIAS: "bench"}))
    changes = np.ensure_alias_labels(core, {})
    assert np.registered_aliases(core) == {}
    assert changes.unlabelled == {"bench": ["node-a"]}


def test_moving_an_alias_is_reported_on_both_sides():
    core = _Core(_Node("node-a", {ALIAS: "bench"}), _Node("node-b"))
    changes = np.ensure_alias_labels(core, {"bench": "node-b"})
    assert np.registered_aliases(core) == {"bench": ["node-b"]}
    assert changes.labelled == {"bench": "node-b"}
    assert changes.unlabelled == {"bench": ["node-a"]}


def test_an_unchanged_registry_patches_nothing():
    core = _Core(_Node("node-a", {ALIAS: "bench"}))
    changes = np.ensure_alias_labels(core, {"bench": "node-a"})
    assert core.patches == []
    assert changes.labelled == {} and changes.unlabelled == {}


def test_one_node_cannot_carry_two_aliases():
    core = _Core(_Node("node-a"))
    with pytest.raises(ValueError, match="at most one"):
        np.ensure_alias_labels(core, {"bench": "node-a", "gpu": "node-a"})
    assert core.patches == []


def test_cleanup_forgetting_placement_keeps_the_alias_registry():
    core = _Core(_Node("node-a", {ALIAS: "bench", np.DATA_NODE_LABEL: np.LABEL_VALUE}))
    np.clear_labels(core)
    assert np.registered_aliases(core) == {"bench": ["node-a"]}


# -- registration ------------------------------------------------------------------------

def test_registration_problems_are_all_reported_at_once():
    core = _Core(_pooled("node-a"), _Node("node-out", {np.NODE_ID_LABEL: "x"}),
                 _pooled("node-cordoned", cordoned=True))
    problems = np.job_node_alias_problems(
        core, {"ok": "node-a", "out": "node-out", "down": "node-cordoned",
               "ghost": "node-missing"}, POOL)
    text = "\n".join(problems)
    assert len(problems) == 3
    assert "'out'" in text and "outside" in text
    assert "'down'" in text and "cordoned" in text
    assert "'ghost'" in text and "no node is called" in text


def test_two_aliases_on_one_node_are_a_registration_problem():
    core = _Core(_pooled("node-a"))
    assert np.job_node_alias_problems(core, {"bench": "node-a", "gpu": "node-a"}, POOL)


def test_a_node_carrying_the_batch_taint_can_be_registered():
    """Campaign jobs tolerate it, so a campaign node may carry it."""
    core = _Core(_pooled("node-a", taints=[_Taint("dedicated", "batch")]))
    assert np.job_node_alias_problems(core, {"bench": "node-a"}, POOL) == []


def test_no_aliases_reads_no_nodes():
    core = mock.Mock()
    assert np.job_node_alias_problems(core, {}, POOL) == []
    core.list_node.assert_not_called()


# -- resolution at campaign start --------------------------------------------------------

def _resolve(core, alias="bench", pool=None):
    return np.resolve_job_node_alias(core, alias, pool=POOL if pool is None else pool)


def test_an_alias_resolves_to_the_nodes_identity_label():
    core = _Core(_pooled("node-a", labels={ALIAS: "bench"}), _pooled("node-b"))
    assert _resolve(core) == "id-node-a"


# Named so that no fixed wording in a message can contain them by accident.
@pytest.mark.parametrize("nodes,cause", [
    pytest.param([_pooled("worker-1", labels={ALIAS: "gpu"})],
                 np.AliasUnresolved.UNREGISTERED, id="unregistered"),
    pytest.param([_pooled("worker-1", labels={ALIAS: "bench"}),
                  _pooled("worker-2", labels={ALIAS: "bench"})],
                 np.AliasUnresolved.AMBIGUOUS, id="ambiguous"),
    pytest.param([_pooled("worker-1", labels={ALIAS: "bench"}, cordoned=True)],
                 np.AliasUnresolved.UNSCHEDULABLE, id="cordoned"),
    pytest.param([_pooled("worker-1", labels={ALIAS: "bench"},
                          taints=[_Taint("gpu-only")])],
                 np.AliasUnresolved.UNSCHEDULABLE, id="untolerated-taint"),
    pytest.param([_Node("worker-1", {ALIAS: "bench", np.NODE_ID_LABEL: "id"})],
                 np.AliasUnresolved.OUTSIDE_POOL, id="outside-pool"),
    pytest.param([_Node("worker-1", {ALIAS: "bench", **POOL})],
                 np.AliasUnresolved.UNIDENTIFIED, id="no-identity-label"),
])
def test_each_way_an_alias_fails_has_its_own_cause_and_names_no_node(nodes, cause):
    core = _Core(*nodes)
    with pytest.raises(np.AliasUnresolved) as exc:
        _resolve(core)
    assert exc.value.cause == cause
    assert exc.value.alias == "bench"
    assert "'bench'" in str(exc.value)
    for node in nodes:
        assert node.metadata.name not in str(exc.value)


def test_an_unregistered_alias_names_the_ones_that_exist_and_where_to_register():
    core = _Core(_pooled("node-a", labels={ALIAS: "gpu"}),
                 _pooled("node-b", labels={ALIAS: "arm"}))
    with pytest.raises(np.AliasUnresolved, match="Registered: arm, gpu") as exc:
        _resolve(core)
    assert ENV in str(exc.value)


def test_the_node_an_alias_resolved_to_goes_to_the_log(caplog):
    core = _Core(_pooled("node-a", labels={ALIAS: "bench"}))
    with caplog.at_level(logging.INFO, logger=np.__name__):
        _resolve(core)
    assert "node-a" in caplog.text


def test_an_empty_pool_admits_any_schedulable_node():
    core = _Core(_Node("node-a", {ALIAS: "bench", np.NODE_ID_LABEL: "id"}))
    assert _resolve(core, pool={}) == "id"


def test_an_illegal_alias_is_a_caller_bug_not_a_registry_state():
    with pytest.raises(ValueError):
        _resolve(_Core(), alias="Not.Legal")


# -- the job pod selector ----------------------------------------------------------------

def test_the_selector_is_existing_then_pool_then_pin():
    selector = np.job_node_selector({"disk": "ssd", "node-pool": "other"}, "id-a", POOL)
    assert selector == {"disk": "ssd", "node-pool": "primary", np.NODE_ID_LABEL: "id-a"}


@pytest.mark.parametrize("existing,node_id,pool,expected", [
    (None, None, {}, {}),
    ({}, None, POOL, POOL),
    (None, "id-a", {}, {np.NODE_ID_LABEL: "id-a"}),
])
def test_the_selector_carries_only_what_was_given(existing, node_id, pool, expected):
    assert np.job_node_selector(existing, node_id, pool) == expected


def test_the_selector_does_not_mutate_its_inputs():
    existing, pool = {"disk": "ssd"}, dict(POOL)
    np.job_node_selector(existing, "id-a", pool)
    assert existing == {"disk": "ssd"} and pool == POOL


# -- the cluster wrappers ----------------------------------------------------------------

def _cluster(monkeypatch, *nodes):
    from robovast.execution.cluster_execution import kube_client

    core = _Core(*nodes)
    monkeypatch.setattr(kube_client, "load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)
    return core


def test_applying_refuses_before_writing_any_label(monkeypatch):
    core = _cluster(monkeypatch, _pooled("node-a", labels={ALIAS: "old"}), _Node("node-out"))
    with pytest.raises(ValueError, match="nothing was registered"):
        np.apply_job_node_aliases({"far": "node-out"}, POOL)
    assert core.patches == []
    assert np.apply_job_node_aliases({}, POOL).unlabelled == {"old": ["node-a"]}


def test_checking_no_aliases_reaches_no_cluster(monkeypatch):
    """Guarded by the suite's cluster fixture: any request here would fail the test."""
    np.check_job_node_aliases({}, POOL)


# -- setup -------------------------------------------------------------------------------

def test_setup_refuses_an_alias_outside_the_pool_before_changing_anything(monkeypatch):
    from robovast.execution.cluster_execution import buildkitd_deploy, node_governor
    from robovast.execution.cluster_execution import service_deploy

    touched = []
    core = _Core(_pooled("node-a"), _Node("node-out", {np.NODE_ID_LABEL: "x"}))
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(cluster_setup, "get_cluster_config", lambda name: mock.Mock())
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        lambda *a, **k: touched.append("governor"))
    monkeypatch.setattr(cluster_setup, "ensure_nvidia_device_plugin",
                        lambda **k: touched.append("gpu"))
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac",
                        lambda **k: touched.append("rbac"))
    monkeypatch.setattr(cluster_setup, "apply_node_id_labels",
                        lambda **k: touched.append("node-id"))
    monkeypatch.setattr(cluster_setup, "apply_job_node_aliases",
                        lambda *a, **k: touched.append("aliases"))
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd",
                        lambda *a, **k: touched.append("buildkit"))

    with pytest.raises(ValueError, match="'far'.*outside"):
        cluster_setup.setup_server(config_name="rke2", namespace="default",
                                   jobs_node_labels=POOL,
                                   job_node_aliases={"far": "node-out", "bench": "node-a"})
    assert touched == [], "the cluster was changed before the aliases were checked"
    assert core.patches == []


def _setup(monkeypatch, result_value=None):
    server = mock.Mock(return_value=result_value or {})
    monkeypatch.setattr(cluster_setup, "setup_server", server)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.buildkitd_deploy.settings_from_env",
        lambda: {})
    result = CliRunner().invoke(cluster_cli.setup, ["rke2"])
    return result, server


def test_setup_applies_the_aliases_the_environment_states(monkeypatch):
    monkeypatch.setenv(ENV, '{"bench": "node-a", "gpu": "node-b"}')
    result, server = _setup(monkeypatch)
    assert result.exit_code == 0, result.output
    assert server.call_args.kwargs["job_node_aliases"] == {"bench": "node-a", "gpu": "node-b"}


def test_setup_without_the_variable_states_no_aliases(monkeypatch):
    """Stated, not omitted: setup reconciles the registry to it and removes the rest."""
    monkeypatch.delenv(ENV, raising=False)
    result, server = _setup(monkeypatch)
    assert result.exit_code == 0, result.output
    assert server.call_args.kwargs["job_node_aliases"] == {}


def test_setup_refuses_a_malformed_variable_before_calling_the_cluster(monkeypatch):
    monkeypatch.setenv(ENV, "bench=node-a")
    result, server = _setup(monkeypatch)
    assert result.exit_code != 0
    assert ENV in result.output
    assert not server.called


@pytest.mark.parametrize("command", [cluster_cli.setup, cluster_cli.upgrade])
def test_aliases_are_not_a_flag(command):
    """One place to state them. A flag beside the ``.env`` is a second source the next
    command, reading only the ``.env``, would silently undo."""
    names = {p.name for p in command.params}
    assert not {"job_node_alias", "forget_job_node_alias", "vast"} & names


def test_setup_prints_every_alias_and_what_changed(monkeypatch):
    changes = np.AliasChanges({"bench": "node-b", "arm": "node-d"}, {"bench": "node-b"},
                              {"bench": ["node-a"], "old": ["node-c"]})
    result, _server = _setup(monkeypatch, {"job_node_aliases": changes})
    assert "job node alias bench: node-a -> node-b" in result.output
    assert "job node alias arm: node-d" in result.output
    assert f"job node alias old: removed from node-c (not in {ENV})" in result.output
