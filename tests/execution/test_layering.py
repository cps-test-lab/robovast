# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Phase D: the execution ENGINE depends on the service in one direction only.

The service composes the engine (``service -> execution``); the engine must not
reach back up into the service. The ``vast exec`` CLI and the wait-and-download
poller under ``execution_utils/`` are client/orchestration code that legitimately
drives the service, so they are deliberately excluded — this guards the *engine*,
where the lone leak (cluster image staging importing a private ``service``
constant) used to live.
"""

import subprocess
import sys

# The engine: dispatch, lifecycle, packing, status recovery, and the whole
# cluster backend. NOT execution_utils (CLI/host-side orchestration), and NOT
# `cluster_execution.cluster_service` -- it now lives under `execution/` because that is
# where the cluster lane is packaged, but it *is* a service binding and imports the
# service layer by design. Membership here is about the role, not the directory.
_ENGINE_MODULES = [
    "robovast.execution.backends",
    "robovast.execution.controller",
    "robovast.execution.control_server",
    "robovast.execution.packer",
    "robovast.execution.status_recovery",
    "robovast.execution.cluster_execution.kubernetes_backend",
    "robovast.execution.cluster_execution.cluster_execution",
    "robovast.execution.cluster_execution.cluster_image_build",
    "robovast.execution.cluster_execution.container_runner",
    "robovast.execution.cluster_execution.kubernetes_kueue",
]


def test_execution_engine_does_not_import_service_at_load():
    """Importing every engine module must not pull ``robovast.service`` into memory."""
    code = (
        "import importlib, sys\n"
        f"for m in {_ENGINE_MODULES!r}:\n"
        "    importlib.import_module(m)\n"
        "bad = [m for m in sys.modules if m == 'robovast.service' "
        "or m.startswith('robovast.service.')]\n"
        "assert not bad, bad\n"
        "print('clean')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "clean" in out.stdout
