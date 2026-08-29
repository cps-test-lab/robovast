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


# -- the first job on a node, before anything is measured -------------------------------


def _calibrated_runner(monkeypatch):
    """A calibrated batch, rendered through the real manifest builder.

    Borrowed wholesale from ``test_job_manifest``: the stubs there are the live cluster
    calls (`the GPU probe, the pull Secret, the registry digest`), none of which this
    question involves, and rendering a real manifest is what the question needs -- the
    defect this pins was invisible to every test that asserted on the sizing helpers
    instead of on the pod that reaches the cluster.
    """
    from tests.execution.test_job_manifest import _runner

    return _runner(monkeypatch, execution={
        "sizing": "calibrated",
        "containers": {"sut": {"image": "an-image"}, "scenario": {}},
    })


def _resources_of(manifest, name):
    """A sidecar is a NATIVE sidecar -- an initContainer with `restartPolicy: Always` -- so
    it is not in `spec.containers` and looking only there finds nothing."""
    spec = manifest["spec"]["template"]["spec"]
    for container in list(spec["containers"]) + list(spec.get("initContainers") or []):
        if container["name"] == name:
            return container.get("resources") or {}
    raise AssertionError(f"no container {name!r}")


def test_the_main_container_takes_the_bootstrap_before_any_node_is_calibrated(monkeypatch):
    """The FIRST job on every node, which is every job until one finishes -- so a guard that
    waits for measured figures leaves the main container unsized for exactly the runs the
    bootstrap exists to carry.

    An empty limit is not merely generous. JOB_TEMPLATE reads AVAILABLE_CPUS and
    AVAILABLE_MEM from `resourceFieldRef: limits.*`, and the downward API substitutes the
    NODE's allocatable for an absent limit -- so the scenario sizes itself to the whole
    machine, and the probe measures a container that was never bounded. Measured on a
    96-core node: the run was told it had 96 cores and 125 GiB.
    """
    runner = _calibrated_runner(monkeypatch)
    manifest = runner.create_job_manifest(runner._build_jobs()[0], total_jobs=1)

    main = manifest["spec"]["template"]["spec"]["containers"][0]
    limits = (main.get("resources") or {}).get("limits") or {}
    assert limits.get("cpu"), "the main container must never reach a node without a cpu limit"
    assert limits.get("memory"), "nor without a memory limit"


def test_the_sidecars_take_it_too(monkeypatch):
    """The half that already worked, pinned beside the half that did not: they go through
    one helper, and the value of that is lost if only one caller is covered."""
    runner = _calibrated_runner(monkeypatch)
    manifest = runner.create_job_manifest(runner._build_jobs()[0], total_jobs=1)

    limits = (_resources_of(manifest, "sut").get("limits")) or {}
    assert limits.get("cpu"), "the sut container must never reach a node without a cpu limit"
    assert limits.get("memory"), "nor without a memory limit"


def test_the_main_container_takes_its_measured_figure_once_the_node_is_calibrated(monkeypatch):
    """The probe records the main container under the name the MONITOR wrote --
    `resource_usage_main.csv`, hence `robovast` -- while the `.vast` calls the container by
    its role. Look the figures up by the role and the lookup misses on every node, and the
    miss is silent: the container falls back to the bootstrap and stays there for the whole
    campaign, throttling against a figure nobody chose. Measured on a 10-run campaign: every
    run quota-bound on the main container while both sidecars used their calibrated values.
    """
    from robovast.execution.cluster_execution.manifests import MAIN_CONTAINER_NAME

    runner = _calibrated_runner(monkeypatch)
    figures = {MAIN_CONTAINER_NAME: {"sustained": 1.386, "peak": 1.754, "samples": 90}}
    manifest = runner.create_job_manifest(runner._build_jobs()[0], total_jobs=1,
                                          node_figures=figures)

    cpu = float((_resources_of(manifest, MAIN_CONTAINER_NAME).get("requests") or {})["cpu"])
    bootstrap_cpu = bootstrap_sizing("scenario")[0]
    assert cpu != bootstrap_cpu, "still on the bootstrap: the measured figure never arrived"
    assert cpu >= 1.386, "the scenario role takes its sustained figure, plus headroom"


def test_the_scenario_bootstrap_clears_what_a_probe_will_measure():
    """The bootstrap is also what the PROBE runs at, so a figure below the container's real
    demand is self-defeating: the probe throttles against its own ceiling, the guard refuses
    it as having measured the ceiling rather than the demand, and no node is ever calibrated
    -- leaving the whole campaign on the bootstrap, which is what calibration exists to
    avoid.

    Measured on a four-node cluster at ``scenario: 1``: every probe refused, at 15.9-20.2%
    throttling. The container is cheap on average -- 0.45-0.76 cores, median 0.36-0.77 --
    but peaks at 1.37-1.40 during bring-up, and it is the PEAK a cap has to clear. The
    figure asserted here is that peak, taken raw from a probe's `system_usage_main.csv`
    rather than from the campaign log, which prints it with `advice.CPU_HEADROOM` applied
    and would overstate it by a quarter.
    """
    observed_peak_cores = 1.40
    assert bootstrap_sizing("scenario")[0] > observed_peak_cores, (
        "a probe capped at or below its own peak measures the cap, and is refused")


def test_the_refuse_ratio_is_tighter_than_a_probe_that_holds():
    """The two numbers have to be read together: the guard refuses above this ratio, so the
    bootstrap above it is only meaningful while the threshold stays small enough that a
    genuinely-clipped probe trips it. Pinned so neither drifts into the other."""
    from robovast.execution.cluster_execution.node_calibration import (
        PROBE_THROTTLE_REFUSE_RATIO)

    assert 0 < PROBE_THROTTLE_REFUSE_RATIO < 0.05, "bring-up noise, not a clipped run"
