# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The runner and everything it spawns can be stopped with SIGINT.

The cluster entrypoint starts the runner as a background job, because the done-marker has to
be written however the runner ends and a foreground child would defer the TERM trap. A shell
without job control sets SIGINT and SIGQUIT to SIG_IGN for such a job, and that disposition
survives every exec below it: the runner, the scenario's children, a bag recorder. Nothing
downstream can undo it -- exec preserves SIG_IGN, ``Popen(restore_signals=True)`` resets only
SIGPIPE/SIGXFZ/SIGXFSZ, and a non-interactive shell may not reset a signal that was ignored on
entry -- so it has to be right where the job is forked.

What it costs when it is not: ``ros2 bag record`` closes its bag on SIGINT and on nothing else.
Ignored, it is killed after a timeout instead, and a bag that was never closed has no
``metadata.yaml``, which makes it unreadable to every converter. The runs still pass, so the
campaign reports success and the trajectories are gone.

The masks are read through /proc, so these tests are Linux-only -- as the containers are.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from robovast.common.execution import render_entrypoint

pytestmark = pytest.mark.skipif(not os.path.isdir('/proc/self'),
                                reason="reads signal masks from /proc")

#: The two the shell wrongly ignores, as bits of the /proc mask.
_IGNORED_BY_A_BACKGROUND_JOB = (1 << (signal.SIGINT - 1)) | (1 << (signal.SIGQUIT - 1))

#: Prints its own ignored-signal mask and that of a child it spawns the way the runner does --
#: through Popen, with no shell in between to apply the rule a second time.
_PROBE = """
import os, subprocess, sys

def ignored(pid):
    with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("SigIgn:"):
                return int(line.split()[1], 16)
    raise AssertionError(f"no SigIgn for {pid}")

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
print("runner", hex(ignored(os.getpid())))
print("child", hex(ignored(child.pid)))
child.kill()
child.wait()
"""


def _post_run_block() -> str:
    """The cluster entrypoint's post-run block, as it is shipped."""
    rendered = render_entrypoint(cluster=True)
    start = rendered.index('    BUILTIN_CLEANUP_SCRIPT=')
    end = rendered.index('    }', rendered.index('    run_scenario() {')) + len('    }')
    return rendered[start:end]


def _run_probe(tmp_path, probe_path, *, env_supports_default_signal=True):
    """Run *probe_path* through the shipped block's ``run_scenario``, as a job would."""
    script = [
        'set -e',
        'log() { :; }',
        'POST_COMMAND=""',
        f'IPC_DIR="{tmp_path}"',
        _post_run_block(),
        f'run_scenario "{sys.executable}" "{probe_path}"',
    ]
    env = dict(os.environ)
    if not env_supports_default_signal:
        # An `env` too old for --default-signal, so the block takes its job-control fallback.
        stub = tmp_path / "bin"
        stub.mkdir(exist_ok=True)
        (stub / "env").write_text('#!/bin/bash\nfor a in "$@"; do\n'
                                  '  case "$a" in --default-signal*) exit 125;; esac\ndone\n'
                                  'exec /usr/bin/env "$@"\n', encoding="utf-8")
        (stub / "env").chmod(0o755)
        env["PATH"] = f"{stub}:{env['PATH']}"
    return subprocess.run(["bash", "-c", "\n".join(script)], capture_output=True, text=True,
                          check=False, env=env)


def _masks(out: subprocess.CompletedProcess) -> dict:
    assert out.returncode == 0, f"stdout={out.stdout}\nstderr={out.stderr}"
    return {line.split()[0]: int(line.split()[1], 16)
            for line in out.stdout.splitlines() if line.startswith(("runner ", "child "))}


def test_the_runner_and_what_it_spawns_can_be_interrupted(tmp_path):
    """Neither the runner nor its child may inherit an ignored SIGINT. The child is the case
    that costs a campaign: it is where the bag recorder sits."""
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    masks = _masks(_run_probe(tmp_path, probe))
    assert masks["runner"] & _IGNORED_BY_A_BACKGROUND_JOB == 0, "the runner ignores SIGINT/SIGQUIT"
    assert masks["child"] & _IGNORED_BY_A_BACKGROUND_JOB == 0, "the runner's children ignore it"


def test_an_env_without_default_signal_still_yields_a_signalable_runner(tmp_path):
    """The flag is coreutils 8.30 and newer. Where it is missing the block turns job control on
    instead, which gives the job its own process group and the same dispositions -- so the
    fallback is tested rather than assumed."""
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    masks = _masks(_run_probe(tmp_path, probe, env_supports_default_signal=False))
    assert masks["runner"] & _IGNORED_BY_A_BACKGROUND_JOB == 0
    assert masks["child"] & _IGNORED_BY_A_BACKGROUND_JOB == 0


def test_the_shell_forwards_term_to_the_runner_and_not_to_a_wrapper(tmp_path):
    """``$!`` has to name the runner itself. Prefixing the job with a command that is not a
    simple command -- a function, a subshell -- makes the trap send the kubelet's TERM to a
    wrapper, and the runner keeps running until the grace period kills it with its files
    half-written."""
    probe = tmp_path / "probe.py"
    probe.write_text("""
import signal, sys, time, pathlib
seen = pathlib.Path(sys.argv[1])
signal.signal(signal.SIGTERM, lambda *_: (seen.write_text("term"), sys.exit(0)))
pathlib.Path(sys.argv[2]).write_text("ready")
time.sleep(30)
""", encoding="utf-8")
    seen, ready = tmp_path / "seen", tmp_path / "ready"
    script = ['set -e', 'log() { :; }', 'POST_COMMAND=""', f'IPC_DIR="{tmp_path}"',
              _post_run_block(),
              f'run_scenario "{sys.executable}" "{probe}" "{seen}" "{ready}"']
    with subprocess.Popen(["bash", "-c", "\n".join(script)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as shell:
        try:
            deadline = time.monotonic() + 30
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), "the runner never started"
            shell.send_signal(signal.SIGTERM)
            shell.wait(30)
        finally:
            if shell.poll() is None:
                shell.kill()
    assert seen.exists(), "the runner was never sent the TERM the shell forwards"
