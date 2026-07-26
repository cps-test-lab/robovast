# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Phase C: the Status contract lives in common and is independent of upper layers."""

import subprocess
import sys


def test_status_reexported_identically_from_control_server():
    """`control_server` must re-export the very same objects (no divergent copy)."""
    from robovast.common import status as common_status
    from robovast.execution import control_server
    for name in ("Status", "Phase", "RunProgress", "BudgetItem", "TERMINAL_PHASES",
                 "RUNNING_PHASES", "is_terminal", "is_running", "failure_detail"):
        assert getattr(common_status, name) is getattr(control_server, name), name


def test_common_status_does_not_import_upper_layers():
    """Importing the contract must not drag in service/execution/etc.

    A fresh interpreter imports only ``robovast.common.status``; none of the upper
    layers may appear in ``sys.modules`` afterwards. This is the guard that keeps
    the foundational contract from silently re-acquiring an upward dependency.
    """
    code = (
        "import robovast.common.status\n"
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
