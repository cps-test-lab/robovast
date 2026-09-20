# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign job node pool -- ``ROBOVAST_JOB_NODE_LABELS`` in the operator's ``.env``.

A property of the CLUSTER rather than of a campaign, carried to the service in
``ROBOVAST_JOB_NODE_LABELS``: a ``nodeSelector`` on each job pod, backed by an admission
controller that counts capacity only inside the same pool.
"""


import pytest

from robovast.execution.cluster_execution import kubernetes_backend as kb
from robovast.execution.cluster_execution.node_placement import (JOB_NODE_POOL_ENV,
                                                                 NODE_ID_LABEL,
                                                                 job_node_pool)


def _manifest(existing=None):
    spec = {}
    if existing:
        spec["nodeSelector"] = dict(existing)
    return {"spec": {"template": {"spec": spec}}}


def _pin(monkeypatch, pool, node_id, existing=None):
    if pool is None:
        monkeypatch.delenv(JOB_NODE_POOL_ENV, raising=False)
    else:
        monkeypatch.setenv(JOB_NODE_POOL_ENV, pool)
    m = kb.BatchJobRunner._pin(kb.BatchJobRunner(), _manifest(existing), node_id)
    return m["spec"]["template"]["spec"].get("nodeSelector")


# -- the contract ------------------------------------------------------------------------

def test_the_pool_reaches_the_pod(monkeypatch):
    """Not just the accounting. The provider counts only nodes in the pool, so a pod free to
    land outside it would run on capacity nothing reserved."""
    assert _pin(monkeypatch, '{"node-pool": "primary"}', None) == {"node-pool": "primary"}


def test_the_per_run_pin_narrows_the_pool_rather_than_replacing_it(monkeypatch):
    """A selector that dropped the pool would defeat the confinement it was placed inside."""
    got = _pin(monkeypatch, '{"node-pool": "primary"}', "node-abc")
    assert got == {"node-pool": "primary", NODE_ID_LABEL: "node-abc"}


def test_what_the_spec_already_carried_survives(monkeypatch):
    got = _pin(monkeypatch, '{"node-pool": "primary"}', "node-abc", existing={"zone": "a"})
    assert got == {"zone": "a", "node-pool": "primary", NODE_ID_LABEL: "node-abc"}


def test_no_pool_configured_is_every_node(monkeypatch):
    assert _pin(monkeypatch, None, None) is None
    assert _pin(monkeypatch, None, "node-abc") == {NODE_ID_LABEL: "node-abc"}


def test_an_empty_value_clears_a_previous_pool(monkeypatch):
    """Setup writes the var on every run, empty included. Without that, omitting the option
    would leave a previously configured pool in force, and the command would stop being the
    whole truth about the cluster it configures."""
    assert _pin(monkeypatch, "", None) is None


# -- fail loudly rather than meaning "every node" -----------------------------------------

@pytest.mark.parametrize("raw", ['{"a": 1}', "[]", "not json", '"a"'])
def test_a_value_that_cannot_be_read_raises(monkeypatch, raw):
    """A typo that silently became "every node" would scatter a campaign across machines the
    operator excluded, and the symptom shows up nowhere near the cause."""
    monkeypatch.setenv(JOB_NODE_POOL_ENV, raw)
    with pytest.raises(ValueError, match=JOB_NODE_POOL_ENV):
        job_node_pool()


# -- a campaign confined to one node: execution.kubernetes.jobs.node ----------------------

MIB = 1024 ** 2
_CONFINED = {"execution": {"kubernetes": {"jobs": {"node": "bench"}}}}


def _resolving_to(monkeypatch, node_id=None, error=None):
    """Stub the alias lookup; returns the list of calls it received."""
    from robovast.execution.cluster_execution import node_placement

    calls = []

    def _resolve(core, alias, *, pool):
        calls.append((alias, dict(pool)))
        if error is not None:
            raise error
        return node_id

    monkeypatch.setattr(node_placement, "resolve_job_node_alias", _resolve)
    return calls


def _unregistered():
    from robovast.execution.cluster_execution.node_placement import AliasUnresolved

    return AliasUnresolved("bench", AliasUnresolved.UNREGISTERED,
                           "job node alias 'bench' is not registered on this cluster. "
                           "Registered: lab-a, lab-b.")


class _Provider:
    def budget(self):
        from robovast.execution.cluster_execution.node_admission import Budget, NodeBudget

        return Budget(nodes=(NodeBudget("node-x", 8.0, 64 * 1024 * MIB),
                             NodeBudget("node-y", 64.0, 64 * 1024 * MIB)))

    def capacities(self):
        from robovast.execution.cluster_execution.node_admission import Capacity

        return [Capacity(8.0, 64 * 1024 * MIB, 0, "node-x"),
                Capacity(64.0, 64 * 1024 * MIB, 0, "node-y")]


class _Submitted(Exception):
    """Ends the batch at the moment the plan reaches the queue -- all these tests need."""


def _launch_runner(monkeypatch, campaign_data, admission=None):
    """A runner stubbed down to its launch decisions, recording every Job it creates."""
    import types

    from robovast.execution.cluster_execution.node_admission import JobSizing

    prepared = []
    monkeypatch.setattr(kb, "prepare_campaign_configs",
                        lambda out_dir, data, cluster=False, instance_type_command=None:
                            prepared.append(out_dir))
    monkeypatch.delenv(JOB_NODE_POOL_ENV, raising=False)

    r = kb.BatchJobRunner()
    r.cluster_config = object()
    r.campaign = "camp-2026-07-17-120000"
    r.namespace = "ns"
    r._batch_tag = "batch-0"
    r.campaign_data = campaign_data
    r.admission = admission
    r.prepared = prepared
    r.created = []
    r.k8s_client = types.SimpleNamespace(
        create_namespaced_secret=lambda namespace, body: None)
    r._ensure_k8s_initialized = lambda: None
    r._write_job_param_files = lambda out_dir, campaign_root=None: None
    r._build_jobs = lambda: [types.SimpleNamespace(index=0, items=[]),
                             types.SimpleNamespace(index=1, items=[])]
    r._jobs_already_done = lambda jobs, root: set()
    r.create_job_manifest = lambda job, total, node_figures=None: {"job": job.index}
    r._job_sizing = lambda job, total, node_figures=None: JobSizing(2.0, MIB)
    r.k8s_batch_client = types.SimpleNamespace(
        create_namespaced_job=lambda namespace, body: r.created.append(body))
    r.get_remaining_jobs = lambda names: []
    r._write_job_links = lambda cr: None
    r.cleanup_jobs = lambda campaign=None: None
    r.cleanup_pods = lambda campaign=None: None
    r._invalidate_restarted_jobs = lambda *a, **kw: None
    r._capture_image_digest = lambda label: None
    return r


def _recording_queue():
    from robovast.execution.cluster_execution.node_admission import AdmissionController

    class _Queue(AdmissionController):
        def __init__(self):
            super().__init__(_Provider(), clock=lambda: 0.0)
            self.preflights = []
            self.submits = []

        def preflight(self, sizing, node_id=None):
            self.preflights.append(node_id)
            return super().preflight(sizing, node_id=node_id)

        def submit(self, owner, items, **kw):
            self.submits.append((owner, kw))
            raise _Submitted()

    return _Queue()


@pytest.mark.parametrize("queued", [True, False], ids=["admission", "no-queue"])
def test_an_unresolvable_alias_refuses_the_campaign_before_any_job_exists(monkeypatch,
                                                                           tmp_path, queued):
    """Refused naming the alias and the aliases that do exist, before a config is written
    or a single Job created, on the queued path and the path without a queue alike."""
    queue = _recording_queue() if queued else None
    r = _launch_runner(monkeypatch, _CONFINED, admission=queue)
    _resolving_to(monkeypatch, error=_unregistered())

    with pytest.raises(kb.CampaignConfigError, match="bench") as err:
        r.run_batch_in_pod(str(tmp_path), "tok")

    assert "Registered: lab-a, lab-b" in str(err.value)
    assert "execution.kubernetes.jobs.node" in str(err.value)
    assert r.created == [], "no Job may exist for a campaign whose alias does not resolve"
    assert r.prepared == [], "refused before anything was written into the campaign"
    if queue is not None:
        assert queue.preflights == [] and queue.submits == []


def test_a_confined_campaign_is_pinned_to_its_node_and_does_not_reserve_it(monkeypatch,
                                                                            tmp_path):
    queue = _recording_queue()
    r = _launch_runner(monkeypatch, _CONFINED, admission=queue)
    calls = _resolving_to(monkeypatch, node_id="node-x")

    with pytest.raises(_Submitted):
        r.run_batch_in_pod(str(tmp_path), "tok")

    owner, kw = queue.submits[0]
    assert owner == r.campaign
    assert kw["pin"] == "node-x" and kw["reserves"] is False
    assert queue.preflights and set(queue.preflights) == {"node-x"}, \
        "judged against its own node's capacity"
    assert len(calls) == 1, "resolved once per runner, however often it is asked"


def test_an_unconfined_campaign_submits_exactly_as_before(monkeypatch, tmp_path):
    queue = _recording_queue()
    r = _launch_runner(monkeypatch, {"execution": {}}, admission=queue)
    calls = _resolving_to(monkeypatch, node_id="node-x")

    with pytest.raises(_Submitted):
        r.run_batch_in_pod(str(tmp_path), "tok")

    _, kw = queue.submits[0]
    assert "pin" not in kw and "reserves" not in kw
    assert queue.preflights == [None]
    assert calls == [], "nothing is resolved for a campaign that names no node"


def test_a_confined_campaign_without_a_queue_still_confines_every_pod(monkeypatch, tmp_path):
    """No queue grants a node there, so the campaign's node is the only thing confining it."""
    r = _launch_runner(monkeypatch, _CONFINED, admission=None)
    _resolving_to(monkeypatch, node_id="node-x")
    r.run_batch_in_pod(str(tmp_path), "tok")
    assert len(r.created) == 2
    for body in r.created:
        assert body["spec"]["template"]["spec"]["nodeSelector"] == {NODE_ID_LABEL: "node-x"}


def test_pin_falls_back_to_the_campaign_node_and_keeps_the_pool(monkeypatch):
    monkeypatch.setenv(JOB_NODE_POOL_ENV, '{"node-pool": "primary"}')
    r = kb.BatchJobRunner()
    r.campaign_data = _CONFINED
    r._ensure_k8s_initialized = lambda: None
    r.k8s_client = object()
    calls = _resolving_to(monkeypatch, node_id="node-x")

    m = r._pin(_manifest({"zone": "a"}), None)
    assert m["spec"]["template"]["spec"]["nodeSelector"] == {
        "zone": "a", "node-pool": "primary", NODE_ID_LABEL: "node-x"}
    assert calls == [("bench", {"node-pool": "primary"})], \
        "the alias is resolved inside the pool, never outside it"

    granted = r._pin(_manifest(), "node-x")
    assert granted["spec"]["template"]["spec"]["nodeSelector"][NODE_ID_LABEL] == "node-x"


def test_probes_are_only_considered_for_the_confined_node(monkeypatch):
    """Whether a probe is worth it is judged against the one node the campaign will use."""
    from robovast.execution.cluster_execution import node_calibration
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    seen = []
    monkeypatch.setattr(node_calibration, "calibration_applies",
                        lambda total, count, growable=False: seen.append(count) or True)
    store = {}

    class _Queue:
        def node_ids(self):
            return ["node-w", "node-x", "node-y"]

        def calibration(self, owner, factory=None):
            return store.setdefault(owner, factory())

        def growable(self):
            return False

    r = kb.BatchJobRunner()
    r.campaign = "camp-2026-07-17-120000"
    r.admission = _Queue()
    r.sizing_mode = "calibrated"
    r._campaign_node = "node-x"

    assert r._probe_node_ids(10) == ["node-x"]
    assert seen == [1], "calibration_applies is asked about one node, not three"
    assert isinstance(store[r.campaign], NodeCalibration)


# -- where setup takes the pool from -----------------------------------------------------

def _setup_cli(monkeypatch, *args, pool_env=None):
    """`vast cluster setup rke2` with ROBOVAST_JOB_NODE_LABELS set to *pool_env*."""
    from unittest import mock

    from click.testing import CliRunner

    from robovast.execution.cluster_execution import cli as cluster_cli
    from robovast.execution.cluster_execution import cluster_setup

    server = mock.Mock(return_value={})
    monkeypatch.setattr(cluster_setup, "setup_server", server)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.buildkitd_deploy.settings_from_env",
        lambda: {})
    monkeypatch.delenv("ROBOVAST_JOB_NODE_ALIASES", raising=False)
    if pool_env is None:
        monkeypatch.delenv(JOB_NODE_POOL_ENV, raising=False)
    else:
        monkeypatch.setenv(JOB_NODE_POOL_ENV, pool_env)
    return CliRunner().invoke(cluster_cli.setup, ["rke2", *args]), server


def test_setup_writes_the_pool_the_environment_states(monkeypatch):
    result, server = _setup_cli(monkeypatch, pool_env='{"node-pool": "primary"}')
    assert result.exit_code == 0, result.output
    assert server.call_args.kwargs["jobs_node_labels"] == {"node-pool": "primary"}
    assert "node-pool=primary" in result.output


def test_setup_without_the_variable_writes_no_pool(monkeypatch):
    result, server = _setup_cli(monkeypatch)
    assert result.exit_code == 0, result.output
    assert server.call_args.kwargs["jobs_node_labels"] == {}
    assert "every node" in result.output


def test_setup_refuses_a_malformed_pool_before_anything_is_applied(monkeypatch):
    result, server = _setup_cli(monkeypatch, pool_env="node-pool=primary")
    assert result.exit_code != 0
    assert JOB_NODE_POOL_ENV in result.output
    assert not server.called


def test_setup_has_no_pool_flag(monkeypatch):
    result, server = _setup_cli(monkeypatch, "--jobs-node-label", "node-pool=primary")
    assert result.exit_code == 2
    assert not server.called
