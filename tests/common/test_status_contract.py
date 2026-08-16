# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Phase C: the Status contract lives in common and is independent of upper layers."""

import subprocess
import sys

import pytest


def test_status_reexported_identically_from_control_server():
    """`control_server` must re-export the very same objects (no divergent copy)."""
    from robovast.client import status as common_status
    from robovast.execution import control_server
    for name in ("Status", "Phase", "RunProgress", "BudgetItem", "TERMINAL_PHASES",
                 "RUNNING_PHASES", "is_terminal", "is_running", "failure_detail"):
        assert getattr(common_status, name) is getattr(control_server, name), name


def test_common_status_does_not_import_upper_layers():
    """Importing the contract must not drag in service/execution/etc.

    A fresh interpreter imports only ``robovast.client.status``; none of the upper
    layers may appear in ``sys.modules`` afterwards. This is the guard that keeps
    the foundational contract from silently re-acquiring an upward dependency.
    """
    code = (
        "import robovast.client.status\n"
        "import sys\n"
        "bad = [m for m in sys.modules if m.startswith(('robovast.service', "
        "'robovast.execution', 'robovast.search', 'robovast.results_processing', "
        "'robovast.mcp_server'))]\n"
        "assert not bad, bad\n"
        "print('clean')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "clean" in out.stdout


def test_campaign_data_module_does_not_import_execution_at_load():
    """`common.campaign_data` must not import `robovast.execution` at module load."""
    code = (
        "import robovast.common.campaign_data\n"
        "import sys\n"
        "assert 'robovast.execution' not in sys.modules and not any(\n"
        "    m.startswith('robovast.execution.') for m in sys.modules), \\\n"
        "    [m for m in sys.modules if m.startswith('robovast.execution')]\n"
        "print('clean')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "clean" in out.stdout


# -- the stall verdict -------------------------------------------------------
#
# Derived in the contract, not per client, so the MCP status and the CLI monitor
# cannot disagree about whether a run is wedged.


def _live(**kw):
    from robovast.client.status import Status
    return Status(phase="running", **kw)


def test_progress_age_is_reported_for_a_live_campaign():
    import time

    from robovast.client.status import stall_report
    report = stall_report(_live(progress_since=time.time() - 120))
    assert 119 <= report["progress_age_s"] <= 122


def test_no_declared_budget_yields_null_never_false():
    """The crucial distinction: "I cannot judge" must not render as "it is fine".
    A two-valued flag returns ``false`` here — a health certificate for a run that
    may already be dead — which is why the verdict is tri-state."""
    import time

    from robovast.client.status import NO_STALL_VERDICT, stall_report
    report = stall_report(_live(progress_since=time.time() - 99999))
    assert report["progress_age_s"] > 0
    assert report["stalled"] is None
    assert report["stall_verdict"] == NO_STALL_VERDICT
    # No invented threshold may leak in as a substitute.
    assert "progress_deadline_s" not in report and "stall_reason" not in report


def test_the_enforcement_backstop_is_never_substituted_for_a_verdict():
    """The cluster force-kills at a one-hour backstop when nothing is declared. Using
    that as the *reporting* reference would call a two-minute pilot healthy for
    fifty-nine minutes, which is the bug this separation exists to prevent."""
    from robovast.common.config import (DEFAULT_RUN_DEADLINE_SECONDS, declared_per_run_seconds,
                                        per_run_deadline_seconds)
    assert declared_per_run_seconds({}) is None                       # reporting
    assert per_run_deadline_seconds({}) == DEFAULT_RUN_DEADLINE_SECONDS  # enforcement
    assert declared_per_run_seconds({"timeout": 90}) == 90
    assert per_run_deadline_seconds({"timeout": 90}) == 90


def test_stalled_once_the_age_passes_the_declared_budget():
    import time

    from robovast.client.status import STALL_NEXT_STEP, stall_report
    report = stall_report(_live(progress_since=time.time() - 700,
                                progress_deadline_s=600))
    assert report["stalled"] is True
    # The verdict must carry the next step: the defect it fixes was that the check
    # had to be remembered.
    assert STALL_NEXT_STEP in report["stall_reason"]
    assert "600" in report["stall_reason"]


def test_not_stalled_while_inside_the_declared_budget():
    import time

    from robovast.client.status import stall_report
    report = stall_report(_live(progress_since=time.time() - 60,
                                progress_deadline_s=600))
    assert report["stalled"] is False and "stall_reason" not in report


def test_a_terminal_campaign_is_never_stalled():
    """Its progress stopped advancing because it is over, which is not a stall."""
    import time

    from robovast.client.status import Status, stall_report
    for phase in ("finished", "failed", "stopped", "crashed"):
        st = Status(phase=phase, progress_since=time.time() - 99999,
                    progress_deadline_s=600)
        assert stall_report(st) == {}, phase


def test_the_durable_outcome_round_trips_the_stall_fields(tmp_path):
    """`outcome.json` is the campaign's terminal record; a field the controller sets
    must survive it or the status changes shape after a service restart."""
    import time

    from robovast.client.status import Status
    from robovast.common.campaign_data import read_execution_outcome, write_execution_outcome
    stamp = time.time() - 30
    write_execution_outcome(tmp_path, Status(phase="running", progress_since=stamp,
                                             progress_deadline_s=900))
    reloaded = read_execution_outcome(tmp_path)
    assert reloaded.progress_deadline_s == 900
    assert reloaded.progress_since == pytest.approx(stamp)
