# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Score a crossing: how close it came, how long it took, and how it went wrong.

**A margin, not a verdict.** ``failure_rate`` is a proportion over N runs, so with 3 runs
it has 4 reachable values -- and against a sharp physical threshold nearly every cell lands
on 0 or 1. Measured on the quadrotor campaign that motivated all of this: 93.8% of
configurations scored exactly 0.0 or 1.0, which is a cliff, and no search climbs a cliff.

A run can fail three ways here and no single quantity covers them: ``min_clearance`` is
*large* for a run that timed out on the far side of the room without ever approaching the
barrier. So the objective is the STL-style minimum over one margin per failure mode ::

    robustness = min( (min_clearance - contact)  / clearance_scale,
                      (timeout - t_trial)        / timeout,
                      (arrival_radius - d_goal)  / path_scale )

Continuous, signed, negative means failed, and it degrades gracefully: a run that merely
*nearly* collided scores just above zero rather than jumping to "passed".

**Each margin is divided by a SCALE, never by its own threshold**, and that distinction is
the whole design. A threshold answers *did it fail*; a scale answers *by how much*.
Dividing by the threshold conflates them, and it plateaus: with the 0.05 m contact
threshold as denominator the clearance margin carried 12x the sensitivity of the goal
margin, so ``min()`` returned whichever margin had the tightest denominator rather than
whichever failure was nearest. Measured on a 48-cell Halton search, the clearance term
decided 31 of 48 cells and 32 of 48 sat on the floor -- a worse cliff than the
``failure_rate`` this objective was built to replace (30 of 48). The ``time`` margin was
the only one already divided by its scale, and the only one that never needed a floor.

So there is no floor. A margin below -1 means what it says -- missed by more than the whole
scale -- and stays ordered against its neighbours, which is what lets an adversarial search
keep descending after it has found its first failure instead of going blind.

**Both margins must be able to reach the same depth**, or the deeper one decides every
score. A refusal leaves the robot the whole traverse short, so its goal margin reaches
(0.6 - 5.0)/5.0 = -0.88. A collision only ever penetrates a few centimetres, because contact
is contact -- so dividing that by the doorway's geometry caps the clearance margin near
-0.16 and `min()` stops being "whichever failure is nearest" and becomes "the goal margin,
whenever the robot did not arrive". Scaled by the range it can actually occupy (~0.1 m), a
full-penetration contact reaches about -1.0 and the two failure modes are comparable, which
is what lets an adversarial search prefer the dangerous one.

**Aggregated worst-case across repetitions**, never averaged -- four clean crossings and
one that clipped the doorway average to "clean", and on a QD archive averaging collapses
the very spread the archive exists to map.
"""

import csv
from pathlib import Path

from robovast.common.campaign_data import read_test_result
from robovast.search.aggregate import aggregate
from robovast.search.extractor import (Extractor, ExtractResult, NoSampleError,
                                       completed_run_dirs)

#: Failure modes, in the order a categorical QD measure declares them. `none` is included
#: so a passing run occupies a cell of its own rather than being absent from the archive:
#: "where does it succeed" is as much of the map as "how does it fail".
FAILURE_MODES = ['none', 'collision', 'timeout', 'goal_miss']


def _row(run_dir: Path, filename: str) -> dict:
    """The single data row of a per-run metrics CSV, or ``{}`` when absent."""
    path = run_dir / filename
    if not path.exists():
        return {}
    with open(path, newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    return rows[0] if rows else {}


def _f(row: dict, key: str, default=None):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return default


class NavExtract(Extractor):
    """Turn a cell's runs into one robustness score plus the QD measures.

    ``params``:
        ``metrics``            per-run CSV written by the postprocessing plugin
        ``contact_threshold``  clearance [m] at which a crossing counts as unsafe
        ``arrival_radius``     [m]; ground-truth distance within which a track counts as
                               arrived. NOT nav2's ``xy_goal_tolerance`` -- see below
        ``timeout``            [s]; matches the scenario's own ``timeout()``
        ``clearance_scale``    [m]; the clearance available in the most permissive
                               configuration -- the SCALE the clearance margin is a
                               fraction of, not a threshold
        ``path_scale``         [m]; the nominal traverse, likewise a scale
        ``aggregate``          ``worst`` (default) / ``quantile`` / ``mean``
    """

    def extract(self, config_dir: Path) -> ExtractResult:
        runs = completed_run_dirs(config_dir)
        if not runs:
            # Not a score of zero. This cell produced no result at all, and any number
            # here would be indistinguishable from a real measurement -- for a minimized
            # margin, 0.0 would look like a cell sitting exactly on the boundary, which is
            # the most interesting thing a boundary search can find. The framework records
            # the cell and carries on.
            raise NoSampleError(
                f"{config_dir}: no run produced a result (no test.xml), so there is no "
                f"crossing to score")

        metrics_file = self.params.get('metrics', 'nav_metrics.csv')
        contact = float(self.params.get('contact_threshold', 0.05))
        # DELIBERATELY not nav2's xy_goal_tolerance, and larger than it. nav2 declares
        # success against its AMCL-ESTIMATED pose at the instant it stops; this is measured
        # in the GROUND-TRUTH frame at the last recorded sample, so it carries the
        # localization error and whatever settling happened after the bag closed. They are
        # different quantities and comparing one against the other's threshold is a category
        # error: measured on a 16-cell grid, runs that PASSED ended 0.23-0.51 m out, so a
        # 0.25 threshold scored six of seven of them at -1, maximum severity. 0.6 separates
        # the observed populations cleanly (passing <= 0.51, failing >= 0.99); re-measure it
        # if the room, the robot or the goal changes.
        goal_tol = float(self.params.get('arrival_radius',
                                         self.params.get('goal_tolerance', 0.6)))
        timeout = float(self.params.get('timeout', 120.0))
        how = self.params.get('aggregate', 'worst')

        # -- the SCALES each margin is divided by, which is not the same thing as its
        # threshold, and confusing the two is what made two earlier versions of this
        # objective plateau.
        #
        # A threshold answers *did it fail*; a scale answers *by how much*, and dividing by
        # the threshold conflates them. With contact = 0.05 m as the denominator the
        # clearance margin carries 20 per metre while the goal margin carries 1.67 -- 12x --
        # so `min()` returned whichever margin had the tightest denominator rather than
        # whichever failure was closest. Measured on a 48-cell Halton search: the clearance
        # term decided the score in 31 of 48 cells, and 32 of 48 landed on the floor.
        #
        # The corroboration is the margin that never misbehaved. `time` was already divided
        # by its scale (the timeout) rather than by a threshold, and across those 48 cells it
        # spanned +0.233 .. +0.863 -- it never once needed a floor. The two margins divided
        # by thresholds are exactly the two that produced plateaus.
        #
        # Both scales below are derived from values this example already declares, so they
        # move when the world does rather than being tuned until the numbers look good:
        #   clearance_scale  the range the CLEARANCE MARGIN can occupy, which is set by how
        #                    far the robot gets INTO an obstacle before contact ends the
        #                    trial -- ~0.1 m measured -- and not by how much room the widest
        #                    doorway offers. A scale has to be the reach of its own term:
        #                    normalising by the geometric maximum (1.6/2 - 0.18 = 0.62 m)
        #                    gives the clearance term a sixth of the goal term's reach, and
        #                    `min()` then returns the goal margin whenever the robot fails to
        #                    arrive -- which ranks a robot that safely stopped short BELOW one
        #                    that hit the pedestrian. See the module docstring.
        #   path_scale       the nominal traverse, |goal - start| from the scenario's two
        #                    poses: (-2.5, 0) -> (2.5, 0) = 5.0 m
        clearance_scale = float(self.params.get('clearance_scale', 0.10))
        path_scale = float(self.params.get('path_scale', 5.0))

        robustness, clearances, durations, modes, recoveries = [], [], [], [], []
        for run_dir in runs:
            row = _row(run_dir, metrics_file)
            passed = read_test_result(run_dir)['success']

            clearance = _f(row, 'min_clearance')
            duration = _f(row, 'duration_s', timeout)
            to_goal = _f(row, 'final_distance_to_goal')
            collided = bool(_f(row, 'collided', 0.0))

            # A recorded collision is a fact about the MEASUREMENT, not an override of
            # the score: contact latched, so the true minimum clearance was <= 0 even
            # where the sampled series missed the instant it happened. Correcting the
            # input keeps the margin arithmetic continuous. Forcing the score to -1
            # instead put a verdict inside a margin -- the very thing this objective
            # exists to avoid -- and flattened 26 colliding cells onto one value whose
            # underlying clearances still ranged over 16%.
            if collided and clearance is not None:
                clearance = min(clearance, 0.0)

            # One margin per way this can go wrong, each a fraction of a comparable
            # physical scale, so the worst of them is genuinely the closest to failing.
            # Sign still means pass/fail: the thresholds stay in the numerator.
            margins = []
            if clearance is not None:
                margins.append((clearance - contact) / clearance_scale)
            margins.append((timeout - duration) / timeout)
            if to_goal is not None:
                margins.append((goal_tol - to_goal) / path_scale)
            # No floor. Once each margin is divided by its scale nothing reaches -1 on its
            # own, so a clamp would only discard order it no longer needs to bound: on the
            # 48 cells that motivated this, re-normalising alone gave 48 distinct values
            # with none at or below -1. A margin below -1 now means what it says -- the run
            # missed by more than the whole scale -- and stays ordered against its
            # neighbours, which is what an adversarial search needs in order to keep
            # descending after it has found the first failure.
            robustness.append(min(margins) if margins else 0.0)

            if clearance is not None:
                clearances.append(clearance)
            durations.append(duration)
            recoveries.append(_f(row, 'recovery_count', 0.0))
            modes.append(self._mode(passed, collided, duration, timeout, to_goal, goal_tol))

        if not clearances:
            # Every run of this cell recorded a pose track but no clearance, which is a
            # misconfigured world rather than an unlucky draw: without that term the
            # robustness margin quietly reduces to time-and-goal, so a near-miss scores
            # exactly like a roomy crossing and the search optimises a flat landscape while
            # every run looks healthy. Not NoSampleError -- that would record the cell and
            # carry on, and this defect affects every cell equally.
            raise ValueError(
                f"{config_dir}: no run recorded a clearance value, so the robustness margin "
                f"cannot include it. Three things have to line up, and the likeliest is the "
                f"last: clearance_monitor declared as a component of the robot in the world; "
                f"/clearance in the scenario's bag_record; and the rosbag->CSV plugins listed "
                f"in *search.postprocessing*, not only in results_processing -- the search "
                f"loop scores each batch before the campaign-level block ever runs.")

        failures = sum(1 for r in runs if not read_test_result(r)['success'])
        return ExtractResult(
            objectives={
                # The optimized quantity, and the only one: `objectives` means the objectives
                # the .vast declared. Reporting the unoptimized values here too used to look
                # free -- they were persisted to objectives_json either way -- but it made a
                # single-objective search look multi-objective to everything downstream, and
                # the scalar `unit.objective` that run_view, runs and the campaign card's
                # trajectory all read was silently left NULL for the whole campaign. They are
                # measurements; they belong below. An adversarial search minimizes this; a
                # boundary search traces its zero.
                'robustness': aggregate(robustness, how=how, higher_is_safer=True),
            },
            measures={
                'min_clearance': aggregate(clearances, how=how) if clearances else 0.0,
                'time_to_goal': aggregate(durations, how=how, higher_is_safer=False),
                'failure_rate': failures / len(runs),
                'recovery_count': aggregate(recoveries, how=how, higher_is_safer=False),
                # Categorical: the archive declares the names, so the worst mode seen
                # across this cell's repetitions is handed back by name.
                'failure_mode': self._worst_mode(modes),
            },
        )

    @staticmethod
    def _mode(passed, collided, duration, timeout, to_goal, goal_tol) -> str:
        """Which of the three ways this run went wrong, or ``none``.

        Ordered, because a run can satisfy more than one: a collision is what happened
        even if the trial also ran long afterwards.
        """
        if collided:
            return 'collision'
        if duration is not None and duration >= timeout * 0.99:
            return 'timeout'
        if not passed or (to_goal is not None and to_goal > goal_tol):
            return 'goal_miss'
        return 'none'

    @staticmethod
    def _worst_mode(modes) -> str:
        """The most severe mode across a cell's repetitions.

        A cell that collided once and passed twice is a colliding cell: the archive should
        hold the behaviour that matters, and "it usually works" is not a defence for a
        robot that sometimes hits a person.
        """
        for mode in ('collision', 'timeout', 'goal_miss'):
            if mode in modes:
                return mode
        return 'none'
