# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""nav2's own opinion about a run, from the stack's operating-rate warning.

**The metric is nav2's, not ours.** ``controller_server`` logs "Control loop missed its
desired rate" when it cannot hold its declared frequency. That is the stack saying it failed
a deadline it set for itself, which is exactly what a health check should report and exactly
what a CPU percentile cannot: measured across a sizing sweep, misses fell 12x between an
allocation that lost 6 runs and one that lost none, while the throttle counter over the same
range moved 1.4x and was not even monotone against it.

**It grades a CAMPAIGN, and the per-run rows exist to be aggregated.** Inside the campaign
that lost 11 runs the per-run count did not predict *which* runs failed -- failing runs
averaged 1.1 misses, passing runs 1.2. That is the correct granularity anyway, because an
allocation is a campaign-level property. So read these rows as ``SUM(value) GROUP BY
config_name`` or across campaigns; a single run's ``warn`` is a symptom, not a verdict.

**Why post-hoc rather than in the scenario.** ``log_check(values: [...]) with: repeat(10)``
already exists and is the established idiom, but it would not have fired on the campaign that
lost 11 runs: the highest count in any single run was 8, and ``repeat(N)`` needs N
tick-separated occurrences *within one action*, so it stays silent at both allocations being
compared. It is a debounce, not a counter -- it can produce yes/no but not 60-vs-0, and here
both are "no". Aborting would also destroy the measurement, since finding a floor needs
degraded runs to *finish*. The scenario form remains right for its own job: abandoning a run
whose control loop is definitively gone, to recover the compute.
"""

from robovast.results_processing.run_health import HealthRow

#: nav2's own wording. Matched as a substring because the logger prefixes it with a node name
#: and appends the achieved rate, and because pinning the whole line would break on a nav2
#: release that changed the suffix while still reporting the same condition.
CONTROL_LOOP_MISS = "Control loop missed its desired rate"

CHECK_NAME = "nav2_control_loop_rate"

#: Any miss at all is worth reporting. Not a guess: at a right-sized allocation a 50-run
#: campaign recorded ZERO across every run, so a miss is not the stack's normal background
#: noise -- it is the condition appearing.
WARN_AT = 1

#: Ten, to agree with the ``repeat(10)`` debounce the scenario idiom uses for the same string.
#: That count is what the scenario treats as a sustained loss of the control loop rather than
#: a degradation, and a post-hoc check that disagreed with the live one about the same
#: evidence would be a second oracle -- the thing rule 1 exists to prevent.
ERROR_AT = 10

#: Every line of the trial and nothing else. ``in_window`` excludes bring-up and teardown --
#: in a packed job, another run's lines entirely -- and ``sim_time`` being non-NULL means the
#: clock was up, so the stack was actually running against a simulated world. The same two
#: columns ``resource_usage`` and ``system_usage`` slice on, rather than a rule invented here.
_COUNT_SQL = f"""
    SELECT config_name, run_id, COUNT(*) AS misses
    FROM run_log
    WHERE in_window = 1
      AND sim_time IS NOT NULL
      AND message LIKE '%{CONTROL_LOOP_MISS}%'
    GROUP BY config_name, run_id
"""

#: Every run the campaign has, so a clean run gets a row saying so. Rule 2: ``ok`` is a row
#: and absence is "not checked" -- a run that never missed and a run whose log was never
#: converted must not look alike, and only the first is evidence.
_RUNS_SQL = "SELECT config_name, run_id FROM runs"


def _level(misses: int) -> str:
    if misses >= ERROR_AT:
        return "error"
    if misses >= WARN_AT:
        return "warn"
    return "ok"


def _detail(misses: int) -> str:
    if misses >= ERROR_AT:
        return (f"nav2's control loop missed its rate {misses} times during the trial -- a "
                "sustained loss rather than a degradation. This run is weak evidence about "
                "the stack: what it did was shaped by not running at its declared rate.")
    if misses >= WARN_AT:
        return (f"nav2's control loop missed its rate {misses} time(s) during the trial. A "
                "right-sized campaign records zero, so this is the condition appearing "
                "rather than background noise -- but per-run counts do not predict which "
                "runs fail. Aggregate over the campaign before concluding anything.")
    return "nav2 held its control loop rate for the whole trial."


class ControlLoopRate:
    """Counts nav2's control-loop-rate misses per run.

    Returns ``[]`` -- not an error -- when the campaign has no ``run_log``. That is a
    campaign whose logs were never converted, or one that is not a nav2 stack at all, and
    both mean *not checked*: writing ``ok`` rows for them would be the exact confusion rule 2
    forbids, a clean bill produced from an absent measurement.
    """

    def __call__(self, conn):
        try:
            counts = {(c, r): m for c, r, m in conn.execute(_COUNT_SQL).fetchall()}
            runs = conn.execute(_RUNS_SQL).fetchall()
        except Exception:  # noqa: BLE001 - no run_log/runs table: not checked, not clean
            return []
        return [
            HealthRow(
                config_name=config_name,
                run_id=run_id,
                check=CHECK_NAME,
                level=_level(counts.get((config_name, run_id), 0)),
                value=float(counts.get((config_name, run_id), 0)),
                unit="misses",
                detail=_detail(counts.get((config_name, run_id), 0)),
            )
            for config_name, run_id in runs
        ]
