# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""When no migration step can carry a config forward, hand it to a human instead of stopping.

Some breaking changes cannot be migrated: a capability is removed and there is nothing to map onto.
The ladder's rule is that a step must then **refuse rather than silently drop** -- a config that
loads cleanly but runs a different experiment is the worst outcome available. That leaves the
question of what the caller does with the refusal, and the answer is a work order: the file
migrated to the point it stopped, with a marker at every decision left, in a workspace where the
ordinary authoring tools apply.

Exercised with a throwaway ladder step, because no real one refuses yet -- and a mechanism whose
only test is that it compiles is a mechanism nobody has run. The property that matters most is that
the result **must not validate**: everything else is convenience, but a runnable half-migration
would silently produce different science.
"""

import pytest
import yaml

from robovast.common.migrations import (MIGRATION_MARKER, UnmigratableConfig,
                                        find_migration_markers, migration_marker, upgrade_config)
from robovast.common import migrations
from robovast.common.migrations import config as ladder


@pytest.fixture(name="removed_capability")
def _removed_capability(monkeypatch):
    """A ladder step that refuses, standing in for a real capability removal."""
    def step(raw):
        out = dict(raw)
        execution = dict(out.get("execution") or {})
        containers = dict(execution.get("containers") or {})
        sut = dict(containers.get("sut") or {})
        sut["resources"] = migration_marker(
            "'GaussianVariation' was removed in version 3; choose a replacement distribution",
            was=sut.get("resources"))
        containers["sut"] = sut
        execution["containers"] = containers
        out["execution"] = execution
        out["version"] = ladder.SUPPORTED_CONFIG_VERSION
        raise UnmigratableConfig(
            "'GaussianVariation' was removed in version 3", partial=out,
            reached=ladder.SUPPORTED_CONFIG_VERSION - 1, capability="GaussianVariation")

    monkeypatch.setattr(ladder, "_MIGRATIONS", [step])
    # Patched in BOTH namespaces, because they are read by different callers: the ladder
    # walks its own module attributes, while validate_config imports the names from the
    # package. Leaving the package's copy alone made this fixture agree with reality only
    # for as long as the real version happened to equal the throwaway one -- and then the
    # config was refused for its VERSION before anything could look at its markers, which
    # is not the property under test.
    for module in (ladder, migrations):
        monkeypatch.setattr(module, "BASELINE_CONFIG_VERSION", 1)
        monkeypatch.setattr(module, "SUPPORTED_CONFIG_VERSION", 2)
    return step


def _old_config():
    return {"version": 1, "metadata": {"name": "old"},
            "execution": {"containers": {"sut": {"image": "reg/x:1", "resources": {"cpu": 2}}},
                          "scenario_file": "s.osc", "runs": 1}}


def test_upgrade_surfaces_the_partial_and_where_it_stopped(removed_capability):
    """The caller's useful move is to hand the partial config to a person, so the refusal has to
    carry it -- a bare "cannot migrate" would be a dead end."""
    with pytest.raises(UnmigratableConfig) as excinfo:
        upgrade_config(_old_config())
    error = excinfo.value
    assert error.capability == "GaussianVariation"
    assert error.partial is not None
    assert error.reached == 1
    assert find_migration_markers(error.partial)


def test_the_marker_says_why_and_what_was_there(removed_capability):
    """Whoever resolves this is reading a file they did not write, about a version of robovast that
    no longer exists. A bare "fix me" would make them go and find the old schema."""
    with pytest.raises(UnmigratableConfig) as excinfo:
        upgrade_config(_old_config())
    marker = excinfo.value.partial["execution"]["containers"]["sut"]["resources"][MIGRATION_MARKER]
    assert "removed in version 3" in marker["reason"]
    assert marker["was"] == {"cpu": 2}


def test_a_config_holding_a_marker_does_not_validate(removed_capability):
    """The property everything else rests on. A partly-migrated config that loaded would run a
    different experiment than the campaign it came from -- worse than any refusal. Refused on the
    STRICT path, so no launch route can reach it, not merely by the validator an author might skip.
    """
    from robovast.common.config import validate_config

    with pytest.raises(UnmigratableConfig) as excinfo:
        upgrade_config(_old_config())
    with pytest.raises(ValueError, match="unresolved migration marker"):
        validate_config(excinfo.value.partial)


def test_the_collect_all_validator_lists_every_marker(tmp_path):
    """Each marker is a separate decision, usually in a different place. A count would make
    somebody go looking; the positions are the useful part."""
    from robovast.common.config_validation import validate_project_file

    raw = {"version": 3, "metadata": {"name": "p"},
           "execution": {"containers": {
               "sut": {"image": "reg/x:1",
                       "provenance": {"source": "s", "revision": "r"},
                       "resources": migration_marker("pick a replacement")},
               "scenario": {}},
               "scenario_file": "s.osc", "runs": 1}}
    vast = tmp_path / "c.vast"
    vast.write_text(yaml.safe_dump(raw), encoding="utf-8")
    (tmp_path / "s.osc").write_text("scenario p:\n    do serial:\n        wait elapsed(1s)\n",
                                    encoding="utf-8")
    problems = [p for p in validate_project_file(str(vast))["problems"]
                if p["stage"] == "migration"]
    assert len(problems) == 1
    assert problems[0]["field"] == "execution.containers.sut.resources"
    assert "pick a replacement" in problems[0]["message"]


def test_markers_are_found_wherever_they_land():
    """A step may leave several, at any depth, including inside a list -- so the search walks
    rather than trusting a step to report its own positions."""
    raw = {"a": migration_marker("one"),
           "b": {"c": [{"d": migration_marker("two")}]},
           "e": "untouched"}
    assert sorted(find_migration_markers(raw)) == [("a", "one"), ("b.c[0].d", "two")]


def test_a_clean_config_has_no_markers():
    assert find_migration_markers({"version": 3, "execution": {"runs": 1}}) == []


def test_retrigger_refuses_and_names_the_workspace_command(tmp_path, removed_capability):
    """The refusal has to be actionable. Describing the situation and stopping is what this
    replaces."""
    from robovast.service import retrigger

    source = tmp_path / "c-2026-01-01-000000"
    (source / "_config").mkdir(parents=True)
    (source / "_config" / "campaign.vast").write_text(yaml.safe_dump(_old_config()),
                                                      encoding="utf-8")
    (source / "_config" / "s.osc").write_text("# scenario\n", encoding="utf-8")
    (source / "_execution").mkdir()
    (source / "_execution" / "execution.yaml").write_text(
        yaml.safe_dump({"execution_type": "local", "images": {"sut": "reg/x:1"},
                        "image_revisions": {"sut": "reg/x@sha256:" + "a" * 64}}),
        encoding="utf-8")

    class Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    with pytest.raises(retrigger.RetriggerRefused) as excinfo:
        retrigger.prepare(source, source.name, workspaces_root=tmp_path / "ws",
                          description_limit=200, request_model=Request)
    message = str(excinfo.value)
    assert "--to-workspace" in message
    assert "GaussianVariation" in message
