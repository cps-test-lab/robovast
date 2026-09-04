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
    figures = {"sut": {"cores": 1.5}}
    got = kb.calibrated_resources({}, "sut", figures, roles=("sut",), bootstrap=True,
                                  settings={"size_on": 100, "limit": "request",
                                            "headroom": {"cpu": 1.0}})
    assert got["cpu"] == pytest.approx(1.5), "measured, not bootstrapped"


# -- the two modes must not both answer ------------------------------------------------


def _cfg(sizing, resources=None):
    from robovast.common.config import ExecutionConfig
    c = {"scenario": {"image": "img:1"}}
    if resources is not None:
        c["scenario"]["resources"] = resources
    return ExecutionConfig(containers=c, runs=1, sizing=sizing)


def test_declared_resources_seed_a_calibrated_campaign():
    """Declaring resources under `calibrated` is no longer an error: they are what the probe
    and every not-yet-measured node run at, and the ceiling a measured figure may not exceed.

    Refusing them meant a stack whose bring-up needs more than the cluster default could not
    be calibrated at all -- the `.env` bootstrap was the only way to state a starting figure,
    and it is a property of the deployment rather than of one campaign."""
    cfg = _cfg("calibrated", resources={"cpu": 4})
    assert cfg.sizing == "calibrated", "no longer an error"
    assert cfg.containers["scenario"].resources.cpu == 4, "and the figure is kept"


def test_a_declared_ceiling_still_caps_a_measured_figure():
    """The seed is also the bound. Calibration sizes a node's jobs DOWN to what they need and
    has no business raising a ceiling its author set."""
    figures = {"sut": {"cores": 8.0, "samples": 90}}
    sized = kb.calibrated_resources({"cpu": 4}, "sut", figures, roles=("sut",),
                                    bootstrap=True,
                                    settings={"size_on": 100, "limit": "request",
                                              "headroom": {"cpu": 1.25}})
    assert float(sized["cpu"]) == 4, "clamped to the declaration, not 8 * 1.25"


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
    assert said and "n1" in said[0] and "cores" in said[0]


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
    figures = {MAIN_CONTAINER_NAME: {"cores": 1.386, "samples": 90}}
    manifest = runner.create_job_manifest(runner._build_jobs()[0], total_jobs=1,
                                          node_figures=figures)

    cpu = float((_resources_of(manifest, MAIN_CONTAINER_NAME).get("requests") or {})["cpu"])
    bootstrap_cpu = bootstrap_sizing("scenario")[0]
    assert cpu != bootstrap_cpu, "still on the bootstrap: the measured figure never arrived"
    assert cpu >= 1.386, "its measured figure, plus headroom"


def test_the_scenario_bootstrap_clears_what_a_probe_will_measure():
    """The bootstrap is also what the PROBE runs at, so a figure below the container's real
    demand is self-defeating: the probe throttles against its own ceiling, the guard refuses
    it as having measured the ceiling rather than the demand, and no node is ever calibrated
    -- leaving the whole campaign on the bootstrap, which is what calibration exists to
    avoid.

    The container is cheap on average and spikes during bring-up, so what a cap has to clear
    is that spike rather than the mean -- a figure chosen from average load looks ample and
    still deadlocks calibration. Asserted against the observed peak so that trimming the
    bootstrap toward the average fails here rather than in a campaign.
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


def test_a_calibrated_container_still_gets_its_memory():
    """Only CPU is calibrated, so every other field has to survive the calibrated path.

    A container with no memory limit is told by the downward API that it has the whole node,
    and `/dev/shm` is sized from the same place -- so an overrun arrives as a SIGBUS with no
    reason attached rather than a clean OOM. The path taken before a node is measured was
    always right, so this is only reachable once calibration succeeds.
    """
    figures = {"scenario": {"cores": 1.33, "memory_peak": 300 * 1024 ** 2, "samples": 90}}
    sized = kb.calibrated_resources({}, "scenario", figures, roles=("scenario",),
                                    bootstrap=True,
                                    settings={"size_on": 95, "limit": "declared",
                                              "headroom": {"cpu": 1.0, "memory": 1.0}})
    assert sized.get("memory"), "a calibrated container must still carry a memory request"
    assert sized.get("memory_limit"), "and a limit, or AVAILABLE_MEM reports the node's"
    assert float(sized["cpu"]) == 1.33, "the CPU figure is the one calibration changes"


def test_calibration_does_not_reintroduce_an_empty_cpu_limit():
    """The infra roles keep a generous ceiling and request the sustained figure -- but with
    nothing declared, `cpu_limit` fell back to the declaration's absent value. An empty
    limit is the same downward-API trap as the memory one, on the other resource."""
    figures = {"simulation": {"cores": 0.9, "samples": 90}}
    sized = kb.calibrated_resources({}, "simulation", figures, roles=("simulation",),
                                    bootstrap=True,
                                    settings={"size_on": 95, "limit": "declared",
                                              "headroom": {"cpu": 1.0}})
    assert sized.get("cpu_limit"), "never empty: an absent limit means the node's capacity"
    assert float(sized["cpu_limit"]) >= float(sized["cpu"]), "a ceiling is not below its floor"


# -- the calibration block: one model, resolved per container ---------------------------


def _runner_with_plan(containers):
    """A runner whose plan is *containers*, each a (name, roles, calibration) triple."""
    import types

    r = kb.BatchJobRunner()
    made = [types.SimpleNamespace(name=n, roles=roles, calibration=cal)
            for n, roles, cal in containers]
    r.plan = types.SimpleNamespace(containers=made, main=made[0], sidecars=made[1:])
    return r


def test_a_container_takes_the_role_rule_when_it_states_nothing():
    """The normal case, and the one almost every campaign should stay in: the system under
    test is read at its maximum and pinned there, everything else at a percentile with room
    to burst."""
    r = _runner_with_plan([("scenario", ("scenario",), None), ("sut", ("sut",), None)])
    settings = r._calibration_by_container()
    assert settings["sut"]["size_on"] == 100 and settings["sut"]["limit"] == "request"
    assert settings["scenario"]["size_on"] == 95
    assert settings["scenario"]["limit"] == "declared"


def test_the_env_default_applies_where_the_vast_is_silent(monkeypatch):
    """A cluster-wide default belongs to the deployment, so it is set once in `.env` rather
    than repeated in every `.vast` that lands on it."""
    monkeypatch.setenv("ROBOVAST_CALIBRATION", '{"sut": {"size_on": 99}}')
    r = _runner_with_plan([("sut", ("sut",), None)])
    assert r._calibration_by_container()["sut"]["size_on"] == 99


def test_the_vast_wins_over_the_env(monkeypatch):
    """Most specific first. The `.env` states what the cluster does by default; a campaign
    that says otherwise is saying something about itself."""
    from robovast.common.config import CalibrationConfig

    monkeypatch.setenv("ROBOVAST_CALIBRATION", '{"sut": {"size_on": 99}}')
    r = _runner_with_plan([("sut", ("sut",), CalibrationConfig(size_on=90))])
    assert r._calibration_by_container()["sut"]["size_on"] == 90


def test_headroom_merges_per_resource_rather_than_replacing(monkeypatch):
    """Stating only `cpu` must keep the memory default rather than dropping it -- the same
    per-field resolution the seed already uses, and for the same reason: half a setting is
    not a decision to discard the other half."""
    from robovast.common.config import CalibrationConfig, CalibrationHeadroom

    r = _runner_with_plan([("sut", ("sut",),
                            CalibrationConfig(headroom=CalibrationHeadroom(cpu=1.6)))])
    headroom = r._calibration_by_container()["sut"]["headroom"]
    assert headroom["cpu"] == 1.6
    assert headroom["memory"], "the memory default survives a cpu-only override"


def test_the_main_container_is_reachable_under_the_name_the_monitor_writes():
    """Its files are `resource_usage_main` whatever the `.vast` calls it, so the settings
    have to be findable under both -- the same aliasing the probe's limits already apply."""
    r = _runner_with_plan([("scenario", ("scenario",), None)])
    settings = r._calibration_by_container()
    assert settings[kb.MAIN_CONTAINER_NAME] == settings["scenario"]


def test_the_tolerance_follows_the_percentile_the_figure_was_read_at():
    """The invariant that must not drift: a container read at 99 tolerates a tenth of the
    clipping one read at 95 does, because clipping cannot move a figure while it stays inside
    the tail the percentile already discards."""
    from robovast.common.config import CalibrationConfig
    from robovast.execution.cluster_execution.node_calibration import probe_refuse_ratio

    r = _runner_with_plan([("sut", ("sut",), CalibrationConfig(size_on=99))])
    pct = r._container_percentiles()["sut"]
    assert pct == 99
    assert probe_refuse_ratio(pct) == pytest.approx(0.01)


# -- a refused probe stops the campaign -------------------------------------------------


def _runner_that_refused(reason, applies=True):
    import types

    r = kb.BatchJobRunner()
    r._calibration_applies = applies
    r.plan = types.SimpleNamespace(containers=[types.SimpleNamespace(name="sut", roles=("sut",),
                                                                    resources=None)],
                                   main=None, sidecars=[])
    calibration = types.SimpleNamespace(
        outcome=lambda: {"calibrated": [], "refused": {"n1": reason} if reason else {}})
    return r, calibration


def test_a_refused_probe_fails_the_campaign():
    """A node that could not be measured would run at the starting allocation while every
    measured node ran at its own figure -- so the campaign silently mixes two sizings, which
    is the inconsistency calibration exists to remove, arriving through the act of failing to
    measure. Raised at the refusal rather than at the end of the batch: every remaining run
    would carry the same fault."""
    from robovast.execution.backends import CampaignConfigError

    r, calibration = _runner_that_refused("its probe was throttled past what its statistic "
                                          "absorbs (sut=71.1%)")
    with pytest.raises(CampaignConfigError) as raised:
        r._refuse_a_probe_that_could_not_measure("n1", calibration)
    message = str(raised.value)
    assert "n1" in message and "throttled" in message
    assert "ROBOVAST_BOOTSTRAP_CPU" in message, "name the remedy for the path it is on"


def test_the_remedy_fits_the_reason_rather_than_the_mode():
    """Five reasons are not one fault with one fix. A probe whose scenario never reached a
    verdict was told to raise an allocation that had nothing to do with it -- and to do so
    "for the container named above", when that reason names no container."""
    r, _ = _runner_that_refused(None)
    verdict = r._remedy_for("its probe reached no verdict")
    assert "resources" not in verdict and "BOOTSTRAP" not in verdict
    assert "_calibration/" in verdict, "point at the probe's own log"

    assert "memory" in r._remedy_for("its probe was OOM-killed (sut)").lower()
    assert "Raise" in r._remedy_for("its probe was throttled past what its statistic absorbs")
    assert "trial" in r._remedy_for("its probe produced fewer than 10 samples")


def test_the_remedy_named_is_the_one_this_campaign_can_act_on():
    """A campaign that declares its own figures is told to raise those; one that declares
    none is told about the deployment default. Naming the wrong one sends a reader to edit a
    file that has no effect on their campaign."""
    import types

    from robovast.execution.backends import CampaignConfigError

    r, calibration = _runner_that_refused("its probe was OOM-killed (sut)")
    r.plan.containers[0].resources = types.SimpleNamespace(cpu=4)
    with pytest.raises(CampaignConfigError) as raised:
        r._refuse_a_probe_that_could_not_measure("n1", calibration)
    assert "execution.containers" in str(raised.value)


def test_a_campaign_calibration_does_not_apply_to_is_untouched():
    """A pilot, or a cluster that can grow: nothing was measured there by design, so there
    are no two sizings to mix and nothing to fail."""
    r, calibration = _runner_that_refused("its probe reached no verdict", applies=False)
    r._refuse_a_probe_that_could_not_measure("n1", calibration)


def test_a_node_with_no_recorded_reason_is_not_a_refusal():
    """`record` also returns False for a stale probe key -- a report about a probe this node
    is no longer running. That is bookkeeping, not a measurement failure, and must not end a
    campaign."""
    r, calibration = _runner_that_refused(None)
    r._refuse_a_probe_that_could_not_measure("n1", calibration)


# -- a probe that lost a container ------------------------------------------------------


def _crash(container="simulation", exit_code=1, restarts=3):
    """What ``restarted_job_forensics`` hands back for one crashed probe."""
    detail = f"container {container} restarted {restarts}x after Error (exit {exit_code})"
    return {"detail": detail,
            "containers": [{"pod_name": "a-probe-pod", "container": container,
                            "role": container, "restart_count": restarts,
                            "reason": "Error", "exit_code": exit_code,
                            "invalidating": True, "detail": detail}]}


def _sweep_runner(monkeypatch, crashed, probes=None):
    """A runner mid-batch with probes out, whose cluster reports *crashed*."""
    import types

    r = kb.BatchJobRunner()
    r.campaign = "camp-1"
    r._batch_tag = "batch-0"
    r._calibration_applies = True
    r.namespace = "ns"
    r.k8s_client = object()
    r._probes = dict({"probe-a": "n1"} if probes is None else probes)
    r.freed, r.deleted, r.captured, r.asked = [], [], [], []
    r._calibration = types.SimpleNamespace(
        outcome=lambda: {"calibrated": [], "refused": {}},
        abandon=lambda node_id, key: r.freed.append((node_id, key)))
    r._delete_job = lambda name: r.deleted.append(name)
    r._capture_container_failures = lambda *a, **k: r.captured.append(a)
    monkeypatch.setattr(
        kb, "restarted_job_forensics",
        lambda core, ns, label, job_names=None: r.asked.append(sorted(job_names or []))
        or crashed)
    return r


def _sweep(runner):
    return runner._fail_on_crashed_probes("a-label", "/campaign", object(), "b", "c/")


def test_a_probe_that_lost_a_container_fails_the_campaign(monkeypatch):
    """A probe's workload containers are native sidecars, so one that dies is RESTARTED
    rather than ending the Job: the probe keeps sampling a stack that keeps dying, holds its
    node while it does, and the campaign has no verdict to report. The probe runs one of the
    campaign's own configurations, so every run would meet the same fault."""
    from robovast.execution.backends import CampaignConfigError

    r = _sweep_runner(monkeypatch, {"probe-a": _crash()})
    with pytest.raises(CampaignConfigError) as raised:
        _sweep(r)
    message = str(raised.value)
    assert "n1" in message and "simulation" in message and "exit 1" in message
    assert r.captured, "the dead container's log outlives its pod by minutes, not hours"
    assert r.deleted == ["probe-a"], "a pinned pod restarting holds its node until deleted"
    assert r.freed == [("n1", "probe-a")] and not r._probes


def test_the_sweep_asks_only_about_this_batch_probes(monkeypatch):
    """The label selector is campaign-wide and finished Jobs linger, so an unscoped answer
    re-reports an earlier batch's restart to every batch that follows."""
    r = _sweep_runner(monkeypatch, {}, probes={"probe-a": "n1", "probe-b": "n2"})
    _sweep(r)
    assert r.asked == [["probe-a", "probe-b"]]


def test_a_probe_still_running_cleanly_is_left_to_its_measurement(monkeypatch):
    """The sweep answers one question -- did a container die -- and a probe that has not
    lost one is still the measurement it was started for."""
    r = _sweep_runner(monkeypatch, {})
    _sweep(r)
    assert r._probes == {"probe-a": "n1"} and not r.deleted


def test_no_outstanding_probe_asks_the_cluster_nothing(monkeypatch):
    """This runs every couple of seconds for the whole batch, and probes are outstanding
    only at its start: past that the question costs a pod list per cycle and answers
    nothing."""
    r = _sweep_runner(monkeypatch, {}, probes={})
    _sweep(r)
    assert r.asked == []


def test_an_unreadable_cluster_leaves_the_probe_to_the_other_door(monkeypatch):
    """Best-effort about the reading: the refusal path judges the same probe a cycle later,
    which is what happened before this sweep existed."""
    r = _sweep_runner(monkeypatch, {})
    monkeypatch.setattr(kb, "restarted_job_forensics",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
    _sweep(r)
    assert r._probes == {"probe-a": "n1"}


def test_the_statistic_defers_to_the_container_that_died():
    """What a crashed probe wrote is a fragment, so the measurement refuses it for whichever
    statistic that fragment fails -- a reason describing the measurement, not the fault. It
    also ends on `execution.sizing: fixed`, which would run the same trial into the same
    crash with the measuring removed."""
    from robovast.execution.backends import CampaignConfigError

    r, calibration = _runner_that_refused("its probe produced fewer than 10 samples")
    r._probe_failures = {"n1": "container simulation restarted 3x after Error (exit 1)"}
    with pytest.raises(CampaignConfigError) as raised:
        r._refuse_a_probe_that_could_not_measure("n1", calibration)
    message = str(raised.value)
    assert "simulation" in message and "exit 1" in message
    assert "lengthen the trial" not in message and "sizing: fixed" not in message


def test_a_node_that_lost_no_container_still_gets_the_sizing_message():
    """The deferral is a precedence, not a replacement: a probe that genuinely could not be
    measured is still a sizing question and still gets the remedy for it."""
    from robovast.execution.backends import CampaignConfigError

    r, calibration = _runner_that_refused("its probe produced fewer than 10 samples")
    with pytest.raises(CampaignConfigError) as raised:
        r._refuse_a_probe_that_could_not_measure("n1", calibration)
    assert "lengthen the trial" in str(raised.value)


# -- what a `.vast` may say, and what it may not -----------------------------------------


def _execution(**kw):
    from robovast.common.config import ExecutionConfig
    return ExecutionConfig(runs=1, **kw)


def test_a_calibration_block_under_fixed_is_refused():
    """Nothing is measured in that mode, so every field in the block decides nothing. Silence
    would let a file read as configured while behaving as default -- the failure this whole
    area keeps producing, arriving through the mechanism added to prevent it."""
    with pytest.raises(ValueError, match="calibration"):
        _execution(sizing="fixed",
                   containers={"sut": {"image": "i", "resources": {"cpu": 1},
                                       "calibration": {"size_on": 99}}})


def test_a_limit_rule_that_ignores_a_declared_ceiling_is_refused():
    """`limit: request` makes the ceiling the measured request, so a declared `cpu_limit` is
    never read. Accepting both leaves a file stating a number nobody honours."""
    with pytest.raises(ValueError, match="cpu_limit"):
        _execution(sizing="calibrated",
                   containers={"sut": {"image": "i", "calibration": {"limit": "request"},
                                       "resources": {"cpu_limit": 4}}})


def test_the_same_pair_is_fine_when_the_ceiling_is_what_is_read():
    """`limit: declared` is exactly the case where a declared ceiling means something."""
    cfg = _execution(sizing="calibrated",
                     containers={"sim": {"image": "i", "calibration": {"limit": "declared"},
                                         "resources": {"cpu": 1, "cpu_limit": 4}}})
    assert cfg.containers["sim"].resources.cpu_limit == 4


def test_a_percentile_outside_its_range_is_refused():
    """Zero would ask for a figure below every sample; above 100 is not a percentile."""
    for bad in (0, 101):
        with pytest.raises(ValueError, match="percentile"):
            _execution(sizing="calibrated",
                       containers={"sut": {"image": "i", "calibration": {"size_on": bad}}})


def test_headroom_below_one_is_refused():
    """It multiplies the measurement, so under 1.0 it would size a container below what it
    was measured using -- which is not headroom in either direction."""
    with pytest.raises(ValueError, match="at least 1.0"):
        _execution(sizing="calibrated",
                   containers={"sut": {"image": "i",
                                       "calibration": {"headroom": {"cpu": 0.9}}}})


# -- the inferred mode has to reach the lane that acts on it -----------------------------


def test_a_campaign_that_declares_nothing_reaches_the_backend_as_calibrated():
    """The inference lives on the model, but what reaches a backend is the PARSED YAML --
    so reading the key alone made every campaign that declared nothing run as `fixed`, which
    is the opposite of what declaring nothing asks for, and silently. Caught by running the
    out-of-the-box case on a cluster, where it was refused by the zero-cpu guard telling it
    to set the mode it had already inferred."""
    from robovast.common.config import infer_sizing

    r = kb.BatchJobRunner()
    r.sizing_mode = infer_sizing({"containers": {"sut": {"image": "i"}, "scenario": {}}})
    assert r._sizing_is_calibrated()


def test_a_campaign_that_declares_a_figure_still_reaches_it_as_fixed():
    """The other half of the same rule: an existing `.vast` keeps meaning what it meant."""
    from robovast.common.config import infer_sizing

    r = kb.BatchJobRunner()
    r.sizing_mode = infer_sizing({"containers": {"sut": {"resources": {"cpu": 3}}}})
    assert not r._sizing_is_calibrated()


def test_the_model_and_the_lane_cannot_answer_differently():
    """Two readings of one rule is how they drift. Pinned against the model's own result so
    a change to either has to change both."""
    from robovast.common.config import ExecutionConfig, infer_sizing

    for containers in ({"sut": {"image": "i"}},
                       {"sut": {"image": "i", "resources": {"cpu": 2}}},
                       {"sut": {"image": "i", "resources": {"memory": "1Gi"}}},
                       {"sut": {"image": "i", "resources": {"gpu": 1}}}):
        model = ExecutionConfig(runs=1, containers=containers)
        assert model.sizing == infer_sizing({"containers": containers}), containers


# -- a probe that was never created is not a probe that failed ---------------------------


def test_a_probe_still_waiting_for_room_is_not_judged():
    """"Not among the jobs still running" is true of a job that ended and equally of one that
    was never created. Read as finished, an unplaceable probe was refused for reaching no
    verdict -- and with a refusal fatal that ended the campaign naming the wrong cause, with a
    remedy that could not have helped. Seen on a node the bootstrap pod does not fit."""
    import types

    from robovast.execution.cluster_execution.node_admission import CREATED, PLANNED

    polled = []

    r = kb.BatchJobRunner()
    r.campaign = "camp-1"
    r._batch_tag = "batch-0"
    r._probes = {"probe-a": "n1", "probe-b": "n2"}
    r._calibration = types.SimpleNamespace(
        outcome=lambda: {"calibrated": [], "refused": {}},
        record=lambda *a, **k: True)
    r.admission = types.SimpleNamespace(
        states=lambda owner: {"probe-a": CREATED, "probe-b": PLANNED},
        finished=lambda name: None)
    r._container_percentiles = lambda: {}
    r._probe_container_files = lambda: {}
    r._probe_container_limits = lambda: {}
    r.get_remaining_jobs = lambda names: polled.append(list(names)) or []

    r._collect_probes(storage=types.SimpleNamespace(read_object=lambda *a: None),
                      bucket_name="b", campaign_prefix="c/")
    assert polled == [["probe-a"]], "the planned probe is never asked about"


def test_nothing_created_yet_polls_nothing():
    """The first cycles of every batch, before the queue has found room for anything."""
    import types

    from robovast.execution.cluster_execution.node_admission import PLANNED

    r = kb.BatchJobRunner()
    r._batch_tag = "batch-0"
    r.campaign = "camp-1"
    r._probes = {"probe-a": "n1"}
    r._calibration = types.SimpleNamespace(outcome=lambda: {"calibrated": [], "refused": {}})
    r.admission = types.SimpleNamespace(states=lambda owner: {"probe-a": PLANNED},
                                        finished=lambda name: None)
    r.get_remaining_jobs = lambda names: (_ for _ in ()).throw(
        AssertionError("must not poll a probe that was never created"))
    r._collect_probes(storage=types.SimpleNamespace(read_object=lambda *a: None),
                      bucket_name="b", campaign_prefix="c/")


# -- a campaign that measured its nodes is not on the bootstrap --------------------------


def _bootstrap_guard_runner(calibrated_nodes, applies=False):
    """A runner past the per-batch gate, with *calibrated_nodes* already measured."""
    import types

    r = kb.BatchJobRunner()
    r.sizing_mode = "calibrated"
    r._calibration_applies = applies
    r._calibration = types.SimpleNamespace(
        outcome=lambda: {"calibrated": list(calibrated_nodes), "refused": {}})
    r._job_index_by_name = {}
    return r


def test_the_bootstrap_guard_leaves_a_campaign_that_calibrated_alone():
    """`_calibration_applies` is recomputed per batch from that batch's job count, while the
    calibration lives for the whole campaign -- so a search whose later batch is smaller than
    the node count flips the flag without un-measuring anything.

    Read alone the flag then says "this campaign is on the bootstrap" of a campaign whose
    nodes were measured in batch 0, and the guard reports the calibrated container sitting at
    its own measured ceiling as a default that does not fit. Observed on a ramping search:
    killed at 8.1% throttling on a node it had itself calibrated, naming a bootstrap it was
    not using."""
    r = _bootstrap_guard_runner(["n1", "n2"])
    # Returns before reading any artifact: with nodes measured there is no bootstrap to judge.
    r._refuse_a_bootstrap_that_did_not_hold(None, "bucket", "prefix/", "job-0")


def test_it_still_guards_a_campaign_that_measured_nothing():
    """The case it exists for -- a pilot, or a cluster that can grow -- where every container
    really is on a cluster-wide default nobody chose for this workload."""
    r = _bootstrap_guard_runner([])
    r._job_index_by_name = {"job-0": 0}
    r._probe_container_files = lambda: {}
    r._probe_container_limits = lambda: {}
    r._container_percentiles = lambda: {}
    r._job_artifact_path = lambda i: f"j{i}"
    # Reaches the read, which is where a real campaign would fetch counters; an unreadable
    # one is not a verdict, so this returns rather than raising.
    r._refuse_a_bootstrap_that_did_not_hold(
        type("S", (), {"read_object": staticmethod(lambda *a: None)})(), "b", "p/", "job-0")


def test_whether_calibration_applies_is_decided_once_for_the_campaign():
    """It compares the work there is against the nodes there are, and a batch is only part of
    the work -- so asking per batch judges a long search by whichever batch was smallest. A
    ramping search then flips to "does not apply" while its nodes stay measured, and
    everything reading the answer is told the campaign is on the bootstrap when it is not."""
    from robovast.execution.cluster_execution.node_calibration import (NodeCalibration,
                                                                       calibration_applies)

    c = NodeCalibration()
    assert c.applies is None, "undecided until the first batch"

    # First batch: plenty of jobs against four nodes.
    c.applies = calibration_applies(total_jobs=15, node_count=4)
    assert c.applies is True

    # A later, smaller batch would answer differently on its own numbers ...
    assert calibration_applies(total_jobs=3, node_count=4) is False
    # ... but the campaign's decision is already made and is not revisited.
    assert c.applies is True


def test_a_node_that_was_never_measured_is_reported():
    """Held while its probe is out, so it takes no work; freed when the probe is abandoned,
    re-probed next batch, stopped by the same thing, held again. The campaign finishes on the
    rest of the cluster and nothing in the results says a machine sat out."""
    import types

    r = kb.BatchJobRunner()
    r._calibration_applies = True
    r._probes = {"probe-a": "n1", "probe-b": "n2"}
    r._calibration = types.SimpleNamespace(
        outcome=lambda: {"calibrated": ["n1"], "refused": {}})
    assert r.unmeasured_nodes() == ["n2"], "n1 was measured; n2 never was"


def test_nothing_outstanding_is_nothing_to_report():
    import types

    r = kb.BatchJobRunner()
    r._calibration_applies = True
    r._probes = {}
    r._calibration = types.SimpleNamespace(
        outcome=lambda: {"calibrated": ["n1", "n2"], "refused": {}})
    assert r.unmeasured_nodes() == []


def test_a_campaign_calibration_never_applied_to_reports_nothing():
    """A pilot holds no node and measures none by design, so there is no machine sitting out."""
    import types

    r = kb.BatchJobRunner()
    r._calibration_applies = False
    r._probes = {"probe-a": "n1"}
    r._calibration = types.SimpleNamespace(outcome=lambda: {"calibrated": [], "refused": {}})
    assert r.unmeasured_nodes() == []


def test_a_node_unmeasured_once_is_counted_not_condemned():
    """The fix for a campaign that dies on a busy moment at its own start.

    At the end of ONE batch, a probe that could never be placed and a probe that lost a race
    for free capacity look identical -- and the first is already refused before any job exists,
    by the preflight in `_start_probes`. So what arrives here is the second, and it drains.
    Counting it lets the next batch settle the question; failing on it does not.
    """
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    calibration = NodeCalibration()
    calibration.applies = True

    # Batch 0: a fresh runner, as a search builds one per round. n2's probe never ran.
    r0 = kb.BatchJobRunner()
    r0._calibration_applies = True
    r0._calibration = calibration
    r0._probes = {"probe-a": "n1", "probe-b": "n2"}
    calibration._by_node["n1"] = {"sut": {"cores": 1.0}}
    assert r0.weigh_unmeasured_nodes() == {"n2": 1}, "counted once, not condemned"

    # Batch 1: a NEW runner and the same calibration -- which is the point of the tally
    # living on the calibration. n2 is stopped by the same thing again.
    r1 = kb.BatchJobRunner()
    r1._calibration_applies = True
    r1._calibration = calibration
    r1._probes = {"probe-c": "n2"}
    assert r1.weigh_unmeasured_nodes() == {"n2": 2}, \
        "a per-batch runner would have read 1 forever"

    # The link between the tally and the verdict, pinned here so the two cannot drift: one
    # batch is below the line and two reach it, which is the whole behaviour change.
    from robovast.execution.cluster_execution.node_calibration import UNMEASURED_BATCH_LIMIT
    assert 1 < UNMEASURED_BATCH_LIMIT <= 2, \
        "one unmeasured batch must warn and not condemn; two must be enough to decide"


def test_weighing_is_what_records_so_the_pure_question_stays_askable():
    """`unmeasured_nodes` has several callers and must not charge a batch as a side effect;
    `weigh_unmeasured_nodes` has one and does. Asking the pure one twice changes nothing."""
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    calibration = NodeCalibration()
    r = kb.BatchJobRunner()
    r._calibration_applies = True
    r._calibration = calibration
    r._probes = {"probe-a": "n1"}

    assert r.unmeasured_nodes() == ["n1"]
    assert r.unmeasured_nodes() == ["n1"]
    assert r.weigh_unmeasured_nodes() == {"n1": 1}, "two pure reads charged nothing"


def test_a_measured_node_never_returns_to_the_tally():
    """Why there is no reset: `claim_probe` refuses a calibrated node, so a node that gets
    measured is never probed or held again and the question cannot come back for it."""
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    calibration = NodeCalibration()
    assert calibration.unmeasured_batch("n1") == 1
    calibration._by_node["n1"] = {"sut": {"cores": 1.0}}
    assert calibration.claim_probe("n1", "probe-z") is False, \
        "a measured node is not probed again, so its tally is never read again"


def test_a_campaign_calibration_never_applied_to_weighs_nothing():
    """A pilot holds no node and measures none by design; there is nothing to charge."""
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    calibration = NodeCalibration()
    r = kb.BatchJobRunner()
    r._calibration_applies = False
    r._calibration = calibration
    r._probes = {"probe-a": "n1"}
    assert r.weigh_unmeasured_nodes() == {}
