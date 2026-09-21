# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign's auxiliary containers end when the campaign is stopped.

A variation that needs a helper image runs it while it composes -- pulling it, then
running a command in it -- and that is the longest a campaign goes before its first run.
The container is the part a flag cannot reach: it is a process somewhere else, under a
client that detaches when it is signalled, so what ends it is removing it (locally) or
giving up on the exec and letting the span's pod go (in-cluster).
"""

import os
import shutil
import stat
import subprocess
import time

import pytest

from robovast.common import config_generation as cg
from robovast.common.errors import CampaignStopped
from robovast.common.variation.container_runner import ContainerSpec, LocalContainerRunner


def _fake_docker(tmp_path, run_body):
    """Put a ``docker`` on PATH that logs its argv, and return ``(bindir, log)``.

    ``run`` behaves as *run_body* says; ``rm`` kills whatever ``run`` is still going,
    which is the linkage the real thing has and the whole reason removing the container is
    what ends the attached client.
    """
    log = tmp_path / "docker.log"
    pidfile = tmp_path / "run.pid"
    script = tmp_path / "bin" / "docker"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "case \"$1\" in\n"
        f'  run) echo $$ > "{pidfile}"; {run_body} ;;\n'
        f'  rm)  [ -f "{pidfile}" ] && kill -9 "$(cat "{pidfile}")" 2>/dev/null ;;\n'
        "esac\n"
        "exit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script.parent, log


def test_a_stopped_campaigns_container_is_removed_rather_than_waited_for(tmp_path,
                                                                        monkeypatch):
    """The removal, not a signal: a signalled ``docker run`` detaches and leaves the
    container running, and an entrypoint that ignores SIGTERM ignores a forwarded one."""
    bindir, log = _fake_docker(tmp_path, "sleep 120")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    runner = LocalContainerRunner(ContainerSpec(image="helper:1"),
                                  should_stop=lambda: True)
    started = time.monotonic()
    try:
        with pytest.raises(CampaignStopped, match="helper:1"):
            runner.run(["do-something"])
    finally:
        runner.close()

    assert time.monotonic() - started < 60, "the container was waited out, not ended"
    calls = log.read_text().splitlines()
    run_call = next(c for c in calls if c.startswith("run "))
    name = run_call.split("--name ")[1].split()[0]
    assert f"rm -f {name}" in calls, calls


def test_a_container_nobody_stops_is_left_alone(tmp_path, monkeypatch):
    """The ordinary composition: no predicate, no watcher, no removal."""
    bindir, log = _fake_docker(tmp_path, "true")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    runner = LocalContainerRunner(ContainerSpec(image="helper:1"))
    try:
        runner.run(["do-something"])
    finally:
        runner.close()

    assert not any(c.startswith("rm -f") for c in log.read_text().splitlines())


def test_a_failing_container_still_fails_the_way_plugins_expect(tmp_path, monkeypatch):
    """A stop and a broken command both exit non-zero, and only the watch tells them
    apart -- so a command nobody stopped must still raise the error plugins catch."""
    bindir, _log = _fake_docker(tmp_path, "exit 3")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    runner = LocalContainerRunner(ContainerSpec(image="helper:1"),
                                  should_stop=lambda: False)
    try:
        with pytest.raises(subprocess.CalledProcessError):
            runner.run(["do-something"])
    finally:
        runner.close()


def test_the_runner_a_campaign_gets_carries_the_campaigns_stop(monkeypatch):
    """The predicate reaches the runner where the runner is BUILT.

    Not through a factory: the fallback that builds the local runner is also what resolves
    a ``family:`` ref and what refuses when there is no docker, and a factory installed to
    carry the flag would have to repeat both.
    """
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/docker")
    token = cg.set_aux_stop_predicate(lambda: True)
    try:
        runner = cg._make_container_runner(ContainerSpec(image="helper:1"),
                                           purpose="variation X")
    finally:
        token.var.reset(token)
    try:
        assert runner._should_stop() is True
    finally:
        runner.close()


def test_a_composition_nobody_can_stop_registers_nothing(monkeypatch):
    """A preview and a CLI run have no campaign, so their containers run to their own end."""
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/docker")
    runner = cg._make_container_runner(ContainerSpec(image="helper:1"), purpose="preview")
    try:
        assert runner._should_stop is None
    finally:
        runner.close()


def test_a_stop_is_not_reported_as_a_broken_plugin():
    """The wrapper around a variation calls everything else a bug in the plugin.

    A stop removes the container the variation was waiting on, so the plugin sees its
    command die -- and named as a ``VariationFailed`` that puts an operator's own request
    on the plugin, and makes a search read the generation as unscorable.
    """
    from robovast.common.variation.base_variation import Variation

    class _Stopped(Variation):
        def __init__(self, *a, **k):  # pylint: disable=super-init-not-called
            pass                      # the base wants a parameter model this test has no use for

        def variation(self, in_configs):
            raise CampaignStopped("auxiliary container stopped by request")

    with pytest.raises(CampaignStopped):
        cg.execute_variation("/tmp", [{}], _Stopped, {}, {}, lambda _line: None,
                             "scenario.osc")
