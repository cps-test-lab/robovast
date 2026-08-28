# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``execution.sizing`` -- whether a reservation is declared or measured.

A core count is a fact about the machine it was measured on, so a shipped ``.vast`` naming
one asserts something it cannot know about the cluster it lands on. ``calibrated`` takes the
figure out of the file and measures it per node instead; ``fixed`` is what a ``.vast`` has
always meant and stays the default.
"""
import pytest

from robovast.execution.cluster_execution import kubernetes_backend as kb
from robovast.execution.cluster_execution.node_calibration import (BOOTSTRAP_CPU_ENV,
                                                                   bootstrap_sizing)


# -- the bootstrap ---------------------------------------------------------------------


def test_the_bootstrap_is_per_role():
    """The three roles want very different amounts, and CPU and memory rank them
    differently: the system under test wants cores, the simulator wants memory. One figure
    for both would starve whichever resource the other role dominates."""
    assert bootstrap_sizing("sut")[0] > bootstrap_sizing("simulation")[0], "cores: sut leads"
    assert bootstrap_sizing("simulation")[1] > bootstrap_sizing("sut")[1], "memory: sim leads"


def test_an_ad_hoc_container_takes_the_small_default():
    """Nothing is known about it, and small is the conservative direction for an unknown: it
    is one probe from a measured figure, while a generous default for every unnamed
    container is what makes a probe unplaceable."""
    assert bootstrap_sizing("aux") == bootstrap_sizing("anything-unnamed")


def test_the_bootstrap_comes_from_the_deployment(monkeypatch):
    """A property of the cluster, not of the campaign -- the same argument that takes the
    figure out of the `.vast`. Whoever set the cluster up is who knows it."""
    monkeypatch.setenv(BOOTSTRAP_CPU_ENV, '{"sut": 6}')
    assert bootstrap_sizing("sut")[0] == 6.0


def test_an_override_keeps_the_roles_it_does_not_name(monkeypatch):
    """Raising one role must not silently drop the others."""
    monkeypatch.setenv(BOOTSTRAP_CPU_ENV, '{"sut": 6}')
    assert bootstrap_sizing("simulation")[0] == 3.0


def test_an_unparseable_bootstrap_raises(monkeypatch):
    """Rather than defaulting: a typo that silently became something else would mis-size
    every job of every calibrated campaign, and the symptom appears nowhere near it."""
    monkeypatch.setenv(BOOTSTRAP_CPU_ENV, "lots")
    with pytest.raises(ValueError, match=BOOTSTRAP_CPU_ENV):
        bootstrap_sizing("sut")


# -- what a container asks for before its node has reported ----------------------------


def test_an_undeclared_container_gets_the_bootstrap_for_its_role():
    got = kb.calibrated_resources({}, "sut", None, bootstrap=True)
    assert (got["cpu"], got["memory"]) == bootstrap_sizing("sut")


def test_the_role_decides_where_there_is_one():
    """A stack bundling its own simulator serves the simulation role from its `sut`
    container -- the same precedence calibrated_resources applies to the statistic."""
    got = kb.calibrated_resources({}, "sut", None, roles=("simulation",), bootstrap=True)
    assert (got["cpu"], got["memory"]) == bootstrap_sizing("simulation")


def test_the_limit_is_never_left_empty():
    """`JOB_TEMPLATE` reads AVAILABLE_CPUS/AVAILABLE_MEM from `limits`, and the downward API
    substitutes the NODE's allocatable for an empty limit -- so the container would be told
    it has the whole machine, and /dev/shm, sized from the same place, would turn an overrun
    into a SIGBUS with no reason attached."""
    got = kb.calibrated_resources({}, "sut", None, bootstrap=True)
    assert got["cpu_limit"] and got["memory_limit"], got


def test_a_declaration_is_never_overwritten_by_the_bootstrap():
    """`fixed` and `calibrated` are exclusive at validation, so a declaration reaching here
    means something else put it there -- and it still wins."""
    got = kb.calibrated_resources({"cpu": "2", "memory": "1Gi"}, "sut", None, bootstrap=True)
    assert got["cpu"] == "2" and got["memory"] == "1Gi"


def test_fixed_mode_is_untouched():
    """The bootstrap is opt-in, so a campaign that declares its sizing behaves exactly as it
    did before this existed -- including getting nothing when it declares nothing."""
    assert kb.calibrated_resources({}, "sut", None) == {}


def test_a_measured_node_beats_the_bootstrap():
    figures = {"sut": {"peak": 1.5, "sustained": 0.5}}
    got = kb.calibrated_resources({}, "sut", figures, roles=("sut",), bootstrap=True)
    assert got["cpu"] == pytest.approx(1.5), "measured, not bootstrapped"


# -- the two modes must not both answer ------------------------------------------------


def _cfg(sizing, resources=None):
    from robovast.common.config import ExecutionConfig
    c = {"scenario": {"image": "img:1"}}
    if resources is not None:
        c["scenario"]["resources"] = resources
    return ExecutionConfig(containers=c, runs=1, sizing=sizing)


def test_declaring_resources_under_calibrated_is_refused():
    """Not overridden. The two answer the same question, and a file stating a number nobody
    honours is worse than one stating nothing -- the same rule this block already applies to
    an unknown key."""
    with pytest.raises(ValueError, match="calibrated"):
        _cfg("calibrated", {"cpu": 2})


def test_the_refusal_names_the_container():
    """A campaign declaring resources on one container of three is the likely shape, and
    "somewhere in execution.containers" would not be actionable."""
    with pytest.raises(ValueError, match="scenario"):
        _cfg("calibrated", {"cpu": 2})


def test_a_gpu_declaration_survives_calibration():
    """A device count is a count, not a rate: nothing measures it, so calibration has no
    answer that could override it."""
    assert _cfg("calibrated", {"gpu": 1}) is not None


def test_fixed_is_the_default_and_keeps_declarations():
    from robovast.common.config import ExecutionConfig
    cfg = ExecutionConfig(containers={"scenario": {"image": "img:1",
                                                   "resources": {"cpu": 2}}}, runs=1)
    assert cfg.sizing == "fixed", "a .vast that says nothing means what it always meant"


# -- a bootstrap that did not hold stops the campaign -----------------------------------


def _runner_on_bootstrap(measured, applies=False):
    """A runner whose campaign asked to be calibrated and was not, with *measured* counters."""
    r = kb.BatchJobRunner()
    r.campaign = "camp-1"
    r.sizing_mode = "calibrated"
    r._calibration_applies = applies
    r._job_index_by_name = {"j-0": 0}
    r._batch_tag = None
    r._probe_container_files = lambda: {"sut": "resource_usage_sut.csv"}
    r._probe_container_limits = lambda: {}
    import robovast.execution.cluster_execution.node_calibration as nc
    r._patched = nc
    return r


def test_an_oom_on_the_bootstrap_stops_the_campaign(monkeypatch):
    """Nobody chose the bootstrap for this workload -- it is a cluster-wide default that
    exists to get the first probe off the ground. A run that dies against it is evidence the
    default does not fit, not evidence about the stack, and every remaining run would carry
    the same fault."""
    from robovast.execution.backends import CampaignConfigError

    r = _runner_on_bootstrap(None)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.node_calibration.read_probe_measurement",
        lambda *a, **k: {"sut": {"oom_kills": 1}})
    with pytest.raises(CampaignConfigError, match="OOM-killed"):
        r._refuse_a_bootstrap_that_did_not_hold(object(), "b", "camp/", "j-0")


def test_heavy_throttling_on_the_bootstrap_stops_the_campaign(monkeypatch):
    from robovast.execution.backends import CampaignConfigError

    r = _runner_on_bootstrap(None)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.node_calibration.read_probe_measurement",
        lambda *a, **k: {"sut": {"throttled_ratio": 0.4}})
    with pytest.raises(CampaignConfigError, match="throttled"):
        r._refuse_a_bootstrap_that_did_not_hold(object(), "b", "camp/", "j-0")


def test_a_calibrated_campaign_is_not_second_guessed(monkeypatch):
    """Where the figure was MEASURED on the node, a run that hits it is reported and kept --
    `run_validity_view`'s job. Discarding it would be worse than labelling it."""
    r = _runner_on_bootstrap(None, applies=True)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.node_calibration.read_probe_measurement",
        lambda *a, **k: {"sut": {"oom_kills": 5}})
    r._refuse_a_bootstrap_that_did_not_hold(object(), "b", "camp/", "j-0")  # must not raise


def test_a_fixed_campaign_is_not_second_guessed(monkeypatch):
    """The author declared it. Same rule."""
    r = _runner_on_bootstrap(None)
    r.sizing_mode = "fixed"
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.node_calibration.read_probe_measurement",
        lambda *a, **k: {"sut": {"oom_kills": 5}})
    r._refuse_a_bootstrap_that_did_not_hold(object(), "b", "camp/", "j-0")


def test_an_unreadable_counter_is_not_a_verdict(monkeypatch):
    """A campaign must never die because a counter could not be fetched."""
    r = _runner_on_bootstrap(None)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.node_calibration.read_probe_measurement",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no storage")))
    r._refuse_a_bootstrap_that_did_not_hold(object(), "b", "camp/", "j-0")


# -- what the operator can read afterwards ---------------------------------------------


def test_the_effective_bootstrap_is_logged_once(caplog):
    """An operator should not have to infer whether they are looking at the defaults or at
    what they configured -- only one of those means the `.env` was picked up."""
    import logging

    import robovast.execution.cluster_execution.node_calibration as ncal
    ncal._BOOTSTRAP_LOGGED.clear()
    with caplog.at_level(logging.INFO, logger="robovast"):
        bootstrap_sizing("sut")
        bootstrap_sizing("simulation")
    said = [r for r in caplog.records if "bootstrap sizing" in r.message]
    assert len(said) == 1, "once per process, not once per container"
    assert "defaults" in said[0].getMessage()


def test_the_calibrated_figures_reach_the_campaign_log(caplog):
    """`add_campaign_log_handler` attaches to the top-level `robovast` logger and captures
    every `robovast.*` record by propagation, so an INFO here lands in the campaign's own
    log -- where a reader asking why two nodes behaved differently will look."""
    import logging

    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    c = NodeCalibration()
    c.claim_probe("n1", "probe-1")
    with caplog.at_level(logging.INFO, logger="robovast"):
        c.record("n1", "probe-1", {"sut": {"sustained": 0.5, "peak": 1.5, "samples": 60}})
    said = [r.getMessage() for r in caplog.records if "calibrated" in r.getMessage()]
    assert said and "n1" in said[0] and "peak" in said[0]
