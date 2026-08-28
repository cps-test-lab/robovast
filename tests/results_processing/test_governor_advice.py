# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A node on a scaling governor runs faster the busier it is, which confounds every
per-node figure a campaign records."""

from robovast.results_processing.advice import WANTED_CPU_GOVERNOR, governor_advice


def _row(node, governor, cpu="Some CPU"):
    return {"node": node, "cpu": cpu, "governor": governor}


def test_a_scaling_governor_is_reported():
    """A node whose clock moves with load means runs on it were not measured against the
    same clock, which the campaign's own numbers cannot show."""
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


def test_the_detail_names_the_setting_and_whose_it_is():
    """The advice has to be actionable by the person reading it, and the action is not in
    RoboVAST: the governor is a host setting, so a reader looking for a `.vast` key would
    find none."""
    detail = governor_advice([_row("node-abc", "powersave")])[0]["detail"]
    assert WANTED_CPU_GOVERNOR in detail, "must name the value to set"
    assert "host setting" in detail, "must say where the change belongs"
