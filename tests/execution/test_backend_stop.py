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

"""``DockerBackend`` stop plumbing.

A batch runs ``run.sh`` (a loop over runs) as a single subprocess. Stopping a
campaign must terminate that process — killing one scenario container only fails
one run and the loop continues. These tests exercise ``_run_watching_stop``
against a stand-in script with the same ``SIGTERM`` cleanup-trap shape as the
generated ``run.sh``.
"""

import os
import signal
import stat
import textwrap
import threading

import pytest

from robovast.execution.backends import DockerBackend
from robovast.execution.control_server import ControllerState


def _script(tmp_path, body: str) -> str:
    path = tmp_path / "fake_run.sh"
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(path)


def test_stop_terminates_run_script_via_sigterm(tmp_path):
    """stop_requested → SIGTERM fires the script's cleanup trap, which exits."""
    marker = tmp_path / "cleaned_up"
    script = _script(tmp_path, f"""
        trap 'touch "{marker}"; exit 130' SIGTERM
        while true; do sleep 0.05; done
    """)

    state = ControllerState()
    state.request_stop()  # already requested before we start waiting

    backend = DockerBackend(state=state)
    backend._STOP_POLL_SECONDS = 0.05  # react fast in the test
    rc = backend._run_watching_stop([script])

    assert rc == 130, "script's SIGTERM trap should exit 130"
    assert marker.exists(), "the SIGTERM cleanup trap must have run"


def test_stop_mid_run_is_observed(tmp_path):
    """A stop requested *after* the script starts is still picked up by the poll."""
    marker = tmp_path / "cleaned_up"
    script = _script(tmp_path, f"""
        trap 'touch "{marker}"; exit 130' SIGTERM
        while true; do sleep 0.05; done
    """)

    state = ControllerState()
    backend = DockerBackend(state=state)
    backend._STOP_POLL_SECONDS = 0.05

    # Request the stop shortly after the wait loop begins.
    threading.Timer(0.15, state.request_stop).start()
    rc = backend._run_watching_stop([script])

    assert rc == 130
    assert marker.exists()


def test_no_stop_runs_to_completion(tmp_path):
    """Without a stop, the script runs to its natural exit and is not signalled."""
    marker = tmp_path / "cleaned_up"
    script = _script(tmp_path, f"""
        trap 'touch "{marker}"; exit 130' SIGTERM
        exit 7
    """)

    state = ControllerState()
    backend = DockerBackend(state=state)
    rc = backend._run_watching_stop([script])

    assert rc == 7, "natural exit code must be preserved"
    assert not marker.exists(), "SIGTERM trap must not fire when no stop requested"


def test_no_state_runs_to_completion(tmp_path):
    """A backend without a state (cluster/default) never terminates the run."""
    script = _script(tmp_path, "exit 3\n")
    rc = DockerBackend()._run_watching_stop([script])
    assert rc == 3


def test_ctrl_c_is_forwarded_to_run_script_session(tmp_path):
    """A terminal Ctrl+C reaches the run script's own SIGINT trap.

    The script runs in a new session (detached from the terminal foreground
    group), so ``_run_watching_stop`` must relay the ``SIGINT`` itself. The
    stand-in mimics ``run.sh``: repeated presses force an exit-130, after which
    the wait loop re-raises ``KeyboardInterrupt`` so the command exits instead of
    evaluating a half-finished campaign.
    """
    marker = tmp_path / "forced_exit"
    script = _script(tmp_path, f"""
        SIGINT_COUNT=0
        handle() {{
            SIGINT_COUNT=$((SIGINT_COUNT + 1))
            if [ $SIGINT_COUNT -ge 2 ]; then
                touch "{marker}"
                exit 130
            fi
        }}
        trap handle SIGINT
        while true; do sleep 0.05; done
    """)

    backend = DockerBackend()
    backend._STOP_POLL_SECONDS = 0.05

    # Deliver two Ctrl+C presses to *this* process; the main thread turns each
    # into a KeyboardInterrupt inside proc.wait(), which the loop forwards on.
    for delay in (0.2, 0.5):
        threading.Timer(delay, os.kill, (os.getpid(), signal.SIGINT)).start()

    with pytest.raises(KeyboardInterrupt):
        backend._run_watching_stop([script])

    assert marker.exists(), "the script's SIGINT trap must have force-exited"
