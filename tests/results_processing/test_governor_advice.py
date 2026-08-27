# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A node on a scaling governor runs faster the busier it is, which confounds every
per-node figure a campaign records."""

from robovast.results_processing.advice import WANTED_CPU_GOVERNOR, governor_advice


def _row(node, governor, cpu="Some CPU"):
    return {"node": node, "cpu": cpu, "governor": governor}


def test_a_scaling_governor_is_reported():
    """Measured on 2026-08-27: one node, one scenario, 0.28 realtime alone against 0.81 with
    five concurrent runs -- a 2.9x spread from load alone, and at the low end the run could
    not finish inside its deadline."""
    advice = governor_advice([_row("node-abc", "powersave")])
    assert len(advice) == 1
    assert advice[0]["kind"] == "cpu_governor_scaling"
    assert advice[0]["severity"] == "warning"
    assert "node-abc" in advice[0]["title"] and "powersave" in advice[0]["title"]


def test_the_wanted_governor_says_nothing():
    assert governor_advice([_row("node-abc", WANTED_CPU_GOVERNOR)]) == []


def test_an_unread_governor_says_nothing():
    """None means NOT READ -- true of any container without /sys mounted through. Inventing a
    verdict from a missing measurement is what this module refuses to do everywhere else."""
    assert governor_advice([_row("node-abc", None)]) == []
    assert governor_advice([]) == []


def test_only_the_offending_nodes_are_named():
    advice = governor_advice([_row("node-good", WANTED_CPU_GOVERNOR),
                              _row("node-bad", "powersave"),
                              _row("node-unknown", None)])
    assert len(advice) == 1
    nodes = [n["node"] for n in advice[0]["evidence"]["nodes"]]
    assert nodes == ["node-bad"], "a correct node must not be reported as a problem"


def test_the_detail_explains_which_direction_the_bias_runs():
    """The counter-intuitive half: a QUIET campaign is the slow case, so a small pilot can sit
    near its timeout where a full sweep is comfortable. A reader who assumes the opposite
    draws exactly the wrong conclusion from a pilot."""
    detail = governor_advice([_row("node-abc", "powersave")])[0]["detail"]
    assert "SLOW case" in detail
    assert WANTED_CPU_GOVERNOR in detail
