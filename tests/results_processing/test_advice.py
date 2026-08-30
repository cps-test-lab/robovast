# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Resource advice: what a campaign's own measurements say the next one should reserve.

Worth testing closely because it is the one thing here that tells a user to CHANGE something.
A wrong metric renders as a slightly wrong chart; a wrong suggestion gets typed into a
``.vast`` and then paid on every job of every sweep that follows.
"""

import re
from pathlib import Path

import pytest

from robovast.results_processing import advice as A


def _usage(container, *, cpu_p95=1.0, cpu_peak=1.5, mem_peak=1024 ** 3, ticks=400,
           core_seconds=400.0):
    return {"container": container, "cpu_p95": cpu_p95, "cpu_peak": cpu_peak,
            "mem_peak": mem_peak, "ticks": ticks, "core_seconds": core_seconds}


def _declared(name, cpu=None, memory=None):
    rows = [{"fullkey": f"$.execution.containers.{name}", "value": None}]
    if cpu is not None:
        rows.append({"fullkey": f"$.execution.containers.{name}.resources.cpu", "value": cpu})
    if memory is not None:
        rows.append({"fullkey": f"$.execution.containers.{name}.resources.memory",
                     "value": memory})
    return rows


def _by_kind(items):
    return {item["kind"]: item for item in items}


# -- the rules ----------------------------------------------------------------------------

def test_cpu_is_sized_on_sustained_use_and_memory_on_the_peak():
    """The two differ because their failures differ: a cpu over-run throttles for one
    scheduling period, a memory over-run is an OOM kill and the run is lost."""
    items = _by_kind(A.resource_advice(
        [_usage("sut", cpu_p95=2.0, cpu_peak=5.0, mem_peak=500 * 1024 ** 2)],
        _declared("sut", cpu=8, memory="4Gi")))
    # cpu: 2.0 x 1.25 = 2.5, already on a quarter. The 5.0 peak is NOT what it is sized on.
    assert items["cpu_over_reserved"]["evidence"]["suggested_pod"] == "2.5"
    # memory: 500Mi x 1.25 = 625Mi -> the next 128Mi step.
    assert items["memory_over_reserved"]["evidence"]["suggested_pod"] == "640Mi"


def test_real_campaign_numbers_reproduce_the_reservation_that_was_applied():
    """From icra-random-recovery-3x5-2026-08-12-23183763, whose .vast was then edited to
    exactly these values -- so this asserts the advice a human acted on."""
    items = _by_kind(A.resource_advice(
        [_usage("robovast", cpu_p95=0.72665, mem_peak=379162624),
         _usage("sut", cpu_p95=2.06715, mem_peak=495919104),
         _usage("simulation", cpu_p95=0.417, mem_peak=171732992)],
        _declared("scenario", cpu=4) + _declared("sut", cpu=4)
        + _declared("simulation", cpu=1)))
    per = items["cpu_over_reserved"]["evidence"]["suggested_per_container"]
    assert per == {"scenario": "1", "sut": "2.75", "simulation": "0.75"}
    assert items["cpu_over_reserved"]["evidence"]["declared_pod"] == "9"
    assert items["cpu_over_reserved"]["evidence"]["suggested_pod"] == "4.5"
    # Halving the pod doubles what fits in a fixed quota, which is the point of saying it.
    assert items["cpu_over_reserved"]["evidence"]["throughput_factor"] == 2.0


def test_the_main_container_is_named_as_the_vast_names_it():
    """`resource_usage` records the main container as `robovast`, a role name that appears
    in no .vast. Advice naming it would send the reader to a line that does not exist."""
    items = _by_kind(A.resource_advice(
        [_usage("robovast", cpu_p95=0.5)], _declared("scenario", cpu=4)))
    assert "scenario" in items["cpu_over_reserved"]["evidence"]["suggested_per_container"]


def test_advice_still_lands_when_the_vast_declares_no_resources_at_all():
    """The campaign that most needs this: nothing reserved, so nothing to compare against --
    and the suggestion stands on the measurement alone."""
    items = _by_kind(A.resource_advice(
        [_usage("robovast", cpu_p95=0.5)], _declared("scenario")))
    assert "cpu_not_declared" in items
    assert "memory_not_declared" in items
    assert "scenario" in items["cpu_not_declared"]["detail"]


def test_an_ordinary_miss_is_not_worth_saying():
    # Reservations are guesses; flagging a 20% miss trains the reader to ignore the advice.
    items = A.resource_advice([_usage("sut", cpu_p95=2.0, mem_peak=1024 ** 3)],
                              _declared("sut", cpu=3, memory="1536Mi"))
    assert [i for i in items if i["kind"].endswith("_over_reserved")] == []


def test_under_reservation_is_a_warning_and_says_which_failure_it_is():
    items = _by_kind(A.resource_advice(
        [_usage("sut", cpu_p95=4.0, mem_peak=4 * 1024 ** 3)],
        _declared("sut", cpu=1, memory="512Mi")))
    assert items["cpu_under_reserved"]["severity"] == "warning"
    assert "throttl" in items["cpu_under_reserved"]["detail"].lower()
    assert "oom" in items["memory_under_reserved"]["detail"].lower()


def test_a_thin_sample_produces_a_reason_rather_than_a_number():
    """A p95 over seven points is the maximum wearing a percentile's name. Real shape: the
    quadrotor campaigns run sub-second scenarios and yield 3 in-window ticks in total."""
    items = _by_kind(A.resource_advice([_usage("robovast", ticks=3)], _declared("scenario")))
    assert list(items) == ["resources_unmeasurable"]
    assert not any("suggested_pod" in i.get("evidence", {}) for i in items.values())


def test_a_partially_thin_campaign_sizes_what_it_can_and_says_what_it_skipped():
    items = _by_kind(A.resource_advice(
        [_usage("sut", cpu_p95=2.0), _usage("sidecar", ticks=4)],
        _declared("sut", cpu=8) + _declared("sidecar", cpu=8)))
    assert "cpu_over_reserved" in items
    assert items["resources_partially_unmeasurable"]["evidence"]["containers"] == ["sidecar"]


def test_no_measurement_says_nothing_at_all():
    # A campaign that was never postprocessed has no resource_usage. Silence is correct;
    # inventing a suggestion from no data is not.
    assert A.resource_advice([], []) == []


# -- the contract -------------------------------------------------------------------------

def test_every_item_is_renderable_without_knowing_its_kind():
    """The property that makes "everything the MCP suggests is visible in the UI" a fact
    about the design rather than a promise: a consumer that has never heard of a `kind` can
    still show `title` and `detail` correctly."""
    items = A.resource_advice(
        [_usage("sut", cpu_p95=2.0), _usage("thin", ticks=2)],
        _declared("sut", cpu=8) + _declared("thin", cpu=8))
    assert items
    for item in items:
        assert set(item) == {"kind", "severity", "title", "detail", "evidence"}
        assert item["severity"] in {"suggestion", "warning"}
        assert item["title"] and not item["title"].endswith(".")
        assert item["detail"].endswith(".")
        assert isinstance(item["evidence"], dict)


#: The web UI computes the same suggestions client-side so the panel can draw them without a
#: round trip. Two implementations of one rule is a drift risk, so the numbers are pinned
#: here: this module is the authority, and the TypeScript must agree with it.
_TS_CONSTANTS = {
    "CPU_HEADROOM": A.CPU_HEADROOM,
    "CPU_GRANULARITY": A.CPU_GRANULARITY,
    "MEM_HEADROOM": A.MEM_HEADROOM,
    "CPU_MIN_TICKS": A.MIN_TICKS,
}


@pytest.mark.parametrize("name,expected", sorted(_TS_CONSTANTS.items()))
def test_the_web_ui_sizes_with_the_same_constants(name, expected):
    source = (Path(__file__).parents[2]
              / "frontend/ui/src/lib/campaignDetails.ts").read_text(encoding="utf-8")
    match = re.search(rf"^export const {name} = ([\d.]+)$", source, re.M)
    assert match, f"{name} is not declared in campaignDetails.ts"
    assert float(match.group(1)) == pytest.approx(expected), (
        f"{name} differs between advice.py and campaignDetails.ts: an agent reading the MCP "
        f"summary and a human reading the Details panel would be told to reserve different "
        f"amounts for the same campaign.")


def test_the_web_ui_rounds_memory_to_the_same_unit():
    source = (Path(__file__).parents[2]
              / "frontend/ui/src/lib/campaignDetails.ts").read_text(encoding="utf-8")
    match = re.search(r"^export const MEM_GRANULARITY_BYTES = (.+)$", source, re.M)
    assert match, "MEM_GRANULARITY_BYTES is not declared in campaignDetails.ts"
    # a TS arithmetic expression, which literal_eval cannot parse
    assert eval(match.group(1).replace("**", "**")) == A.MEM_GRANULARITY_BYTES  # noqa: S307  # pylint: disable=eval-used


# -- formatting ---------------------------------------------------------------------------

@pytest.mark.parametrize("cores,text", [(4, "4"), (4.0, "4"), (2.5, "2.5"), (0.75, "0.75")])
def test_cores_are_formatted_as_they_would_be_typed(cores, text):
    assert A.format_cores(cores) == text


@pytest.mark.parametrize("num_bytes,text", [
    (1024 ** 3, "1Gi"), (2 * 1024 ** 3, "2Gi"), (512 * 1024 ** 2, "512Mi"),
    (640 * 1024 ** 2, "640Mi"),
])
def test_memory_is_formatted_as_a_vast_would_write_it(num_bytes, text):
    assert A.format_memory(num_bytes) == text


# -- the shared /dev/shm, which is charged to the pod --------------------------------------

def _shm(size):
    return [{"fullkey": "$.execution.shm_size", "value": size}]


def test_the_memory_suggestion_covers_the_shared_shm_not_just_process_memory():
    """``/dev/shm`` is one memory-backed tmpfs mounted into every container, so its pages
    are charged to the POD. Sizing from RSS alone advised a total the tmpfs by itself could
    fill -- and overrunning shared memory kills with SIGBUS (exit 135), not a clean OOM, so
    the death arrives with nothing attached to explain it."""
    gib = 1024 ** 3
    items = _by_kind(A.resource_advice(
        [_usage("sut", mem_peak=gib)],
        _declared("sut", cpu=1, memory="8Gi") + _shm("1Gi")))

    item = items["memory_over_reserved"]
    # 1 GiB peak x 1.25 headroom = 1.25 GiB, plus the 1 GiB tmpfs the pod must also hold.
    assert item["evidence"]["suggested_pod"] == "2304Mi"
    assert item["evidence"]["shm_size"] == "1Gi"
    assert "shm_size" in item["detail"]
    # The per-container figure stays process memory -- that is what a container limit sizes.
    assert item["evidence"]["suggested_per_container"]["sut"] == "1280Mi"


def test_cpu_advice_is_untouched_by_shm_size():
    """There is no shared CPU allowance, so the tmpfs must not leak into the cpu figure."""
    items = _by_kind(A.resource_advice(
        [_usage("sut", cpu_p95=1.0)],
        _declared("sut", cpu=8, memory="8Gi") + _shm("1Gi")))
    assert items["cpu_over_reserved"]["evidence"]["suggested_pod"] == "1.25"
    assert "shm_size" not in items["cpu_over_reserved"]["evidence"]


def test_a_campaign_that_declares_no_shm_size_is_advised_exactly_as_before():
    """The addition must be invisible to every campaign that does not set it."""
    usage, declared = [_usage("sut", mem_peak=1024 ** 3)], _declared("sut", cpu=1, memory="8Gi")
    with_key = _by_kind(A.resource_advice(usage, declared))["memory_over_reserved"]
    assert with_key["evidence"]["suggested_pod"] == "1280Mi"
    assert "shm_size" not in with_key["evidence"]


# -- sizing execution.shm_size from what the pool actually held ---------------------------
#
# The measured half of the same subject. Every case below turns on one question: did this
# campaign use shared memory at all? Where it did not, the right output is nothing.

_MIB = 1024 ** 2
_GIB = 1024 ** 3


def _measured(peak=None, limit=None):
    return [{"shm_peak": peak, "shm_limit": limit}]


def test_no_advice_when_the_pool_was_never_measured():
    """NULL is a campaign recorded before the monitor sampled the pool, or a runtime with no
    /dev/shm. Neither is "used none of it", so neither may produce a size.

    The empty-list case is a data.db built before the columns existed: the query fails and
    ``data_access.rows`` hands back ``[]``, which has to read as "nothing to say" rather than
    take out the whole summary the advice is one key of.
    """
    assert A.shm_advice(_measured(None, None), _shm("1Gi")) == []
    assert A.shm_advice([], _shm("1Gi")) == []


@pytest.mark.parametrize("peak", [0, 4 * _MIB, 64 * _MIB])
def test_a_campaign_that_does_not_use_shared_memory_is_told_nothing(peak):
    """Shared memory is not always used: a single-container run, a non-DDS middleware, or
    nodes co-located in one process all touch almost none of it. A peak that fits in what the
    smallest lane hands out for free needs no declaration and no reduction -- so there is
    nothing to say, including nothing about a declaration that is larger than it needs."""
    assert A.shm_advice(_measured(peak, 64 * _MIB), []) == []
    assert A.shm_advice(_measured(peak, _GIB), _shm("1Gi")) == []


def test_a_campaign_that_uses_the_pool_and_declares_nothing_is_warned():
    """It survives on the cluster, where an undeclared pool is sized from the pod's limits or
    the node, and dies on the local lane's 64Mi -- of SIGBUS, which is not an OOM kill."""
    item = _by_kind(A.shm_advice(_measured(700 * _MIB, 8 * _GIB), []))["shm_not_declared"]
    assert item["severity"] == "warning"
    # 700Mi x 1.25 = 875Mi, rounded up to the next 128Mi.
    assert item["evidence"]["suggested"] == "896Mi"
    assert item["evidence"]["peak"] == "700Mi"
    assert "declared" not in item["evidence"]


def test_a_declaration_the_peak_is_closing_on_is_warned():
    item = _by_kind(A.shm_advice(_measured(900 * _MIB, _GIB),
                                _shm("1Gi")))["shm_under_reserved"]
    assert item["severity"] == "warning"
    assert item["evidence"]["suggested"] == "1152Mi"
    assert item["evidence"]["declared"] == "1Gi"


def test_a_declaration_far_above_the_peak_is_a_suggestion_not_a_warning():
    """Over-declaring costs the pod's memory budget on every job, not a dead run."""
    item = _by_kind(A.shm_advice(_measured(100 * _MIB, 4 * _GIB),
                                _shm("4Gi")))["shm_over_reserved"]
    assert item["severity"] == "suggestion"
    assert item["evidence"]["suggested"] == "128Mi"
    assert item["evidence"]["throughput_factor"] > 1


def test_a_declaration_that_covers_the_peak_without_waste_says_nothing():
    assert A.shm_advice(_measured(700 * _MIB, _GIB), _shm("1Gi")) == []


def test_every_item_carries_the_limit_the_run_actually_saw():
    """Which is how "did my declaration take effect?" is answered without a rule of its own:
    a declared size the mount never got is otherwise indistinguishable from an honoured one."""
    item = _by_kind(A.shm_advice(_measured(900 * _MIB, 64 * _MIB),
                                _shm("1Gi")))["shm_under_reserved"]
    assert item["evidence"]["observed_limit"] == "64Mi"
    assert item["evidence"]["declared"] == "1Gi"


def test_a_fallback_to_udp_reads_as_under_reserved_not_over_reserved():
    """The ordering matters. A peak that is small BECAUSE the cap was too small sits at its
    cap, so it is caught as under-reserved -- it must never be advised to shrink further."""
    kinds = _by_kind(A.shm_advice(_measured(64 * _MIB + 1, 64 * _MIB), _shm("64Mi")))
    assert "shm_under_reserved" in kinds
    assert "shm_over_reserved" not in kinds


# -- the same number means different things in the two sizing modes ----------------------


_THROTTLED = [{"container": "sut", "periods": 1000, "throttled": 105, "runs": 150,
               "runs_throttled": 150}]
_DECLARED = [{"container": "sut", "cpu": "3"}]


def test_throttling_is_a_warning_when_the_figure_was_declared():
    """A container held at a ceiling its author chose may simply have been given too little,
    and raising that ceiling is a thing the author can do."""
    got = A.throttle_advice(_THROTTLED, _DECLARED, sizing="fixed")
    assert got and got[0]["severity"] == "warning"
    assert "resources.cpu" in got[0]["detail"]


def test_it_is_only_a_suggestion_when_the_figure_was_measured():
    """A container sized AT its own measurement sits against that measurement, so this fires
    on every calibrated campaign -- measured, 150 runs of 150. Left at `warning` it would
    train the reader to skip the one place the same number does mean something."""
    got = A.throttle_advice(_THROTTLED, _DECLARED, sizing="calibrated")
    assert got and got[0]["severity"] == "suggestion"
    assert "Expected under calibrated sizing" in got[0]["title"]


def test_the_remedy_named_is_one_that_would_change_anything():
    """Under `calibrated` the limit IS the node's measurement, so raising the declared ceiling
    changes nothing: what has to grow is the margin above the measurement."""
    got = A.throttle_advice(_THROTTLED, _DECLARED, sizing="calibrated")
    assert "calibration.headroom.cpu" in got[0]["detail"]
    assert "resources.cpu -- sizing on sustained" not in got[0]["detail"]


def test_a_campaign_that_recorded_no_mode_reads_as_the_mode_it_had():
    """`sizing` predates nothing in a campaign recorded before the key existed, and every
    campaign then had declared sizing."""
    got = A.throttle_advice(_THROTTLED, _DECLARED, sizing=None)
    assert got[0]["severity"] == "warning"
