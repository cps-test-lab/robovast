# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which commit composed a campaign — the question a re-run a year later actually asks.

``robovast_version`` looked like it already answered this and does not: it falls back to the
installed package semver when the git lookup fails, so a campaign can record ``2.0.0`` and
carry no revision at all. ``code_revision`` is closer but returns a *short* sha, which is not
a durable identifier, and folds ``+dirty`` into the same string.

What is asserted here is mostly about honesty: the sha must be full when it is a real sha,
the source must say whether it is, a dirty tree must be recorded as such rather than inferred
later from an absent field, and "cannot tell" must stay expressible.
"""

import logging

import pytest

from robovast.common.execution import (GIT_REVISION_ENV, MAX_RECORDED_CHANGED_PATHS,
                                       _provenance_yaml, campaign_code_provenance,
                                       code_provenance)


def test_a_source_checkout_reports_a_full_sha():
    """A short sha is not a durable identifier -- `git checkout` on an abbreviation can
    become ambiguous as a repository grows, and this record has to survive a year."""
    record = code_provenance()
    if record.get("revision_source") != "git":
        pytest.skip("not running from a git checkout")
    assert len(record["revision"]) == 40
    assert set(record["revision"]) <= set("0123456789abcdef")


def test_a_baked_revision_is_used_verbatim_and_labelled(monkeypatch):
    """In an image there is no .git to ask, so the baked value is the only answer. It is
    SHORT, and `revision_source` is what stops a reader mistaking that for a truncation."""
    monkeypatch.setenv(GIT_REVISION_ENV, "abc1234")
    record = code_provenance()
    assert record == {"revision": "abc1234", "revision_source": "baked", "dirty": False}


def test_a_baked_dirty_marker_becomes_a_flag(monkeypatch):
    """`+dirty` is the build's own report. Unpacked rather than left inside the identifier,
    so nobody tries to `git checkout abc1234+dirty`."""
    monkeypatch.setenv(GIT_REVISION_ENV, "abc1234+dirty")
    record = code_provenance()
    assert record["revision"] == "abc1234"
    assert record["dirty"] is True


def test_no_answer_is_an_empty_record(monkeypatch):
    """An empty dict means "this deployment cannot tell you", and every writer must be able
    to record that instead of substituting something that reads as an identifier."""
    monkeypatch.setenv(GIT_REVISION_ENV, "  ")
    monkeypatch.setattr("subprocess.check_output",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert code_provenance() == {}


def test_changed_paths_are_capped_but_the_count_is_not(monkeypatch):
    """A dirty tree can hold thousands of paths. A record that balloons stops being read, so
    the sample is capped -- but the true total is kept, because "how dirty" is the fact."""
    many = "".join(f" M file_{i:04d}.py\n" for i in range(100))
    monkeypatch.delenv(GIT_REVISION_ENV, raising=False)
    monkeypatch.setattr("subprocess.check_output",
                        lambda args, **k: "a" * 40 if "rev-parse" in args else many)
    record = code_provenance()
    assert record["dirty"] is True
    assert record["changed_count"] == 100
    assert len(record["changed_paths"]) == MAX_RECORDED_CHANGED_PATHS


def test_a_dirty_checkout_warns_loudly(monkeypatch, caplog):
    """Warn, do not refuse: running from a dirty tree is the normal research loop, and
    blocking it would only teach people around the check. What must not happen is the
    campaign looking reproducible afterwards."""
    monkeypatch.setenv(GIT_REVISION_ENV, "abc1234+dirty")
    with caplog.at_level(logging.WARNING):
        record = campaign_code_provenance()
    assert record["dirty"] is True
    assert "DIRTY" in caplog.text
    assert "cannot be reproduced" in caplog.text


def test_an_unknowable_revision_warns_too(monkeypatch, caplog):
    monkeypatch.setenv(GIT_REVISION_ENV, "")
    monkeypatch.setattr("subprocess.check_output",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    with caplog.at_level(logging.WARNING):
        assert campaign_code_provenance() == {}
    assert "cannot determine which robovast revision" in caplog.text


def test_a_clean_checkout_says_nothing(monkeypatch, caplog):
    monkeypatch.setenv(GIT_REVISION_ENV, "abc1234")
    with caplog.at_level(logging.WARNING):
        campaign_code_provenance()
    assert caplog.text == ""


def test_provenance_yaml_round_trips():
    """Both lanes write execution.yaml differently -- one dumps a dict, the other emits text
    from a shell script -- so the text form has to parse back to the same record."""
    import yaml

    record = {"revision": "a" * 40, "revision_source": "git", "dirty": True,
              "changed_count": 2, "changed_paths": ["a.py", "b.py"]}
    parsed = yaml.safe_load(_provenance_yaml(record))
    assert parsed == {f"robovast_{key}": value for key, value in record.items()}


def test_provenance_yaml_of_nothing_is_nothing():
    """An unknowable revision must add no keys at all, rather than keys holding null: an
    absent field reads as "not recorded", a null one as "recorded as nothing"."""
    assert _provenance_yaml({}) == ""


def test_the_local_lane_writes_parseable_provenance(monkeypatch, tmp_path):
    """Execute the generated shell, because that is the only thing that proves the heredoc.

    The local lane does not write execution.yaml from Python -- it emits a shell script that
    does, inside the run. So a bad quote or an unindented list item here produces a file that
    parses as something else entirely, and no amount of inspecting the Python would show it.
    Asserting on the *parsed* result also pins that ``dirty`` survives as a bool rather than
    the string "true".
    """
    import subprocess

    import yaml

    from robovast.common.execution import generate_execution_yaml_script

    monkeypatch.setenv(GIT_REVISION_ENV, "deadbee+dirty")
    script = generate_execution_yaml_script(3, {}, output_dir_var="$OUT",
                                            role_images={"scenario": "img:1"})
    sh = tmp_path / "run.sh"
    sh.write_text(f'#!/bin/bash\nset -e\nOUT="{tmp_path}"\nEXECUTION_TIME=now\n'
                  f'DOCKER_IMAGE=img:1\n{script}')
    subprocess.run(["bash", str(sh)], check=True, capture_output=True)

    parsed = yaml.safe_load((tmp_path / "_execution" / "execution.yaml").read_text())
    assert parsed["robovast_revision"] == "deadbee"
    assert parsed["robovast_revision_source"] == "baked"
    assert parsed["robovast_dirty"] is True
    assert parsed["runs"] == 3


def test_the_store_keeps_unknown_dirty_as_null(tmp_path):
    """A campaign recorded before this field existed did not have a clean tree -- it had an
    unknown one. Storing 0 would assert something nobody checked, and every "which results
    came from a dirty tree?" query would then quietly count it as clean."""
    from robovast.common.store import CampaignStore

    with CampaignStore(tmp_path / "c.db") as store:
        legacy = store.create_campaign("c-2025-01-01-000000", "batch")
        store.record_execution(legacy, {"robovast_version": "2.0.0"})
        current = store.create_campaign("c-2026-01-01-000000", "batch")
        store.record_execution(current, {"robovast_version": "abc1234",
                                         "robovast_revision": "a" * 40,
                                         "robovast_revision_source": "git",
                                         "robovast_dirty": True})
        rows = dict(store._conn.execute(  # pylint: disable=protected-access
            "SELECT id, robovast_dirty FROM campaign").fetchall())
    assert rows[legacy] is None
    assert rows[current] == 1
