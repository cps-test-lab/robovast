# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The nav_search objective's margins are commensurable, unfloored, and ordered.

Three properties, each of which a shipped version of this extractor violated, and each
caught only by running a campaign rather than by reading the code:

1. **Divided by a scale, not a threshold.** A threshold answers *did it fail*; a scale
   answers *by how much*. With the 0.05 m contact threshold as denominator the clearance
   margin carried 12x the sensitivity of the goal margin, so ``min()`` returned whichever
   margin had the tightest denominator rather than whichever failure was nearest -- the
   clearance term decided 31 of 48 cells on a real Halton search.
2. **No floor.** Clamping at -1 put 32 of those 48 cells on one value while their
   underlying clearances still ranged over 16%, which is a worse cliff than the
   ``failure_rate`` this objective exists to replace.
3. **A collision corrects the input, not the score.** Forcing -1 on contact put a verdict
   inside a margin -- the very thing being replaced.
"""

import csv
import importlib.util
import pathlib

import pytest

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "configs" / "examples" / "nav_search"


def _extractor_cls():
    if not (EXAMPLE / "search" / "nav_extract.py").is_file():
        pytest.skip("nav_search example not present")
    spec = importlib.util.spec_from_file_location(
        "nav_extract_under_test", EXAMPLE / "search" / "nav_extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NavExtract


def _run(config_dir, index, *, clearance, duration, to_goal, collided, passed=True):
    """One run directory shaped the way the postprocessing plugin leaves it."""
    run_dir = config_dir / f"{index}"
    run_dir.mkdir(parents=True)
    with open(run_dir / "nav_metrics.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "min_clearance", "duration_s", "final_distance_to_goal", "collided",
            "recovery_count"])
        writer.writeheader()
        writer.writerow({"min_clearance": clearance, "duration_s": duration,
                         "final_distance_to_goal": to_goal,
                         "collided": 1 if collided else 0, "recovery_count": 0})
    verdict = "" if passed else '<failure message="x"/>'
    (run_dir / "test.xml").write_text(
        '<?xml version="1.0"?>\n'
        f'<testsuite tests="1" failures="{0 if passed else 1}" errors="0">'
        f'<testcase name="t">{verdict}</testcase></testsuite>\n', encoding="utf-8")
    return run_dir


def _score(tmp_path, name, **run_kwargs):
    config_dir = tmp_path / name
    _run(config_dir, 0, **run_kwargs)
    extractor = _extractor_cls()()
    return extractor.extract(config_dir).objectives["robustness"]


# -- property 1: commensurable ----------------------------------------------

def test_a_metre_of_clearance_and_a_metre_of_goal_error_weigh_comparably(tmp_path):
    """Not identically -- the scales differ because the quantities do -- but within an
    order of magnitude, so `min()` reflects which failure is nearest rather than which
    denominator is smallest.

    Under threshold denominators this ratio was 12x (1/0.05 against 1/0.6).
    """
    cls = _extractor_cls()
    extractor = cls()
    # 0.10 m of clearance given up, against 0.10 m of goal error incurred.
    clearance_cost = 0.10 / float(extractor.params.get("clearance_scale", 0.62))
    goal_cost = 0.10 / float(extractor.params.get("path_scale", 5.0))
    ratio = clearance_cost / goal_cost
    assert 1.0 <= ratio <= 10.0, (
        f"a metre of clearance weighs {ratio:.1f}x a metre of goal error; the margins are "
        f"not commensurable, so min() picks the tightest denominator")


def test_the_time_margin_is_a_fraction_of_the_timeout(tmp_path):
    """The one margin that was always scale-divided, and the one that never plateaued.

    Tested where it BINDS, which is the lesson from getting this wrong: the goal margin is
    ceilinged at ``arrival_radius / path_scale`` = 0.12, because a run cannot end better
    than at the goal. So a comfortable run is always scored by its goal margin, and asking
    for a time margin of 0.5 asked for something ``min()`` can never return.

    That ceiling is not a defect -- it is the goal criterion honestly being the tightest
    constraint here: 0.6 m of slack out of a 5 m traverse really is 12%. And it does not
    hide the clearance signal, because a genuine near-miss drives the clearance margin
    below it and takes over the min, which is exactly what a min-of-margins is for.
    """
    score = _score(tmp_path, "nearly-timed-out", clearance=5.0, duration=114.0,
                   to_goal=0.1, collided=False)
    assert score == pytest.approx((120.0 - 114.0) / 120.0, abs=1e-6)


def test_a_near_miss_takes_over_the_min_from_the_goal_margin(tmp_path):
    """The property that makes the scalar worth optimising: as clearance approaches the
    contact threshold its margin drops through the comfortable run's goal margin, so the
    search feels the near-miss rather than the arrival."""
    comfortable = _score(tmp_path, "roomy", clearance=0.40, duration=30.0, to_goal=0.3,
                         collided=False)
    near_miss = _score(tmp_path, "grazed", clearance=0.055, duration=30.0, to_goal=0.3,
                       collided=False)
    assert 0.0 < near_miss < comfortable, (
        f"a near miss did not register: {near_miss} vs {comfortable}")


# -- property 2: no floor ---------------------------------------------------

def test_a_worse_failure_scores_strictly_worse_with_no_floor(tmp_path):
    """The property an adversarial search needs: having found a failure, it must still be
    able to tell a worse one from a bad one. The clamp made every deep failure identical,
    so a minimiser went blind exactly when it succeeded."""
    near = _score(tmp_path, "near", clearance=0.04, duration=30.0, to_goal=0.0,
                  collided=False)
    deep = _score(tmp_path, "deep", clearance=-0.09, duration=30.0, to_goal=0.0,
                  collided=False)
    deeper = _score(tmp_path, "deeper", clearance=-0.50, duration=30.0, to_goal=0.0,
                    collided=False)
    assert deeper < deep < near, f"not ordered: {deeper} < {deep} < {near}"
    # And nothing stops a margin passing its own scale: -1 is not a floor, it is just the
    # point where a run missed by the whole scale. Under the clamp every one of these was
    # the same number.
    past_scale = _score(tmp_path, "past", clearance=-0.90, duration=30.0, to_goal=0.0,
                        collided=False)
    assert past_scale < -1.0, f"a margin past its scale was floored: {past_scale}"


def test_a_stranded_run_stays_ordered_against_a_slightly_worse_one(tmp_path):
    """Two runs that both gave up far from the goal, 0.5 m apart. Under the old
    `1 - d/arrival_radius` both scored -1; the search could not prefer either."""
    a = _score(tmp_path, "a", clearance=5.0, duration=30.0, to_goal=3.0, collided=False,
               passed=False)
    b = _score(tmp_path, "b", clearance=5.0, duration=30.0, to_goal=3.5, collided=False,
               passed=False)
    assert b < a, f"stranded runs collapsed onto one value: {a} vs {b}"


# -- property 3: the oracle corrects the input ------------------------------

def test_contact_with_a_positive_sampled_clearance_still_fails(tmp_path):
    """`contact_monitor` latched, but the 30 Hz clearance series never dipped -- a fast
    pass can touch between samples. The clearance used becomes <= 0, so the score is
    negative, without a verdict being written into the margin."""
    score = _score(tmp_path, "missed", clearance=0.13, duration=30.0, to_goal=0.0,
                   collided=True)
    assert score < 0.0, "a latched contact scored as a pass"


def test_contact_does_not_flatten_runs_onto_one_value(tmp_path):
    """Scored on the outcome alone, 26 colliding cells share exactly -1.0 while their
    clearances differ by 16%. Correcting the input instead of the score keeps them apart."""
    shallow = _score(tmp_path, "shallow", clearance=-0.01, duration=30.0, to_goal=0.0,
                     collided=True)
    deep = _score(tmp_path, "deepc", clearance=-0.09, duration=30.0, to_goal=0.0,
                  collided=True)
    assert deep < shallow, f"collisions flattened: {deep} vs {shallow}"


def test_the_worst_margin_is_the_one_reported(tmp_path):
    """A run that crossed cleanly but timed out is scored by its time, not its clearance."""
    score = _score(tmp_path, "slow", clearance=5.0, duration=120.0, to_goal=0.0,
                   collided=False, passed=False)
    assert score == pytest.approx(0.0, abs=1e-6)
