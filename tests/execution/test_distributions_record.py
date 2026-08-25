# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every container records which distributions it holds, and what they register.

"Whose code supplied this campaign's assets" is an entry-point question -- a world or a model
comes from whichever distribution registers a provider group -- and only the container can
answer it, because the packages are installed in its image and nowhere else. The record was
being built instead by walking the interpreter of whatever process prepared the campaign, which
on a cluster lane is the service pod: it carries no simulator, so it found nothing and wrote "no
asset providers" for a campaign whose image had three private ones.

Per container, not per pod, and that is the point rather than thoroughness: in the ROS shape the
simulator runs in a container of its own, so every asset provider a campaign used is installed
there and in no other. The main container's record would name none of them.
"""

import json
from importlib.resources import files

import pytest

import robovast.execution.data.collect_sysinfo as collector

SCRIPTS = ("entrypoint.sh", "secondary_entrypoint.sh")


def _script(name: str) -> str:
    return files("robovast.execution.data").joinpath(name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_container_records_its_distributions(name):
    """Both entrypoints, because a record only the main container writes is a record that
    misses the simulator in the shape where the simulator is a separate container."""
    assert "--distributions" in _script(name), (
        f"{name} does not record its distributions")


def test_the_record_is_named_per_container():
    """Following resource_usage_${CONTAINER_NAME}.csv: two containers writing one filename in a
    shared /out is one container's answer silently overwriting the other's."""
    assert "distributions_${CONTAINER_NAME}.json" in _script("secondary_entrypoint.sh")
    assert "distributions_main.json" in _script("entrypoint.sh")


def test_a_sidecar_records_packages_but_not_host_facts():
    """The pod's host facts are the same for every container and the main one already recorded
    them; the packages are what differ."""
    secondary = _script("secondary_entrypoint.sh")
    assert "--no-sysinfo" in secondary
    # And it must not be able to fail the run: a sidecar may be a stock image that mounts
    # nothing, and this is a note about the run, not part of it.
    assert "|| true" in secondary.split("--distributions")[0].rsplit("\n", 2)[0] + \
        secondary.split("--distributions")[1].split("\n")[1]


def test_what_a_distribution_record_contains():
    """version + groups is what identifies a provider; direct_url is what makes it obtainable,
    since for a VCS install it carries the commit."""
    found = collector.get_distributions()
    assert found, "no distributions found in the test environment"
    entry = found.get("robovast") or next(iter(found.values()))
    assert "version" in entry and "groups" in entry
    # This distribution is installed from a working tree, so it has a direct URL.
    assert "direct_url" in (found.get("robovast") or {})


def test_recording_never_fails_a_run(tmp_path, capsys):
    """A fact about a run must not become the reason one fails."""
    collector.write_distributions(str(tmp_path / "missing-dir" / "d.json"))
    assert "could not record installed distributions" in capsys.readouterr().out


def test_the_record_is_json_a_reader_can_load(tmp_path):
    out = tmp_path / "d.json"
    collector.write_distributions(str(out))
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict) and loaded
