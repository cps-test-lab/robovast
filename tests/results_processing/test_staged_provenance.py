# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Provenance survives postprocessing that ran somewhere else.

On the cluster lane a campaign's postprocessing happens in two stages: the rosbag
conversions run in a Kubernetes Job, and everything after them runs as a normal host
pass **with the rosbag steps skipped**. Neither half recorded the conversions -- the Job
was never given a ``--provenance-file``, and the host pass had skipped the steps -- so a
cluster campaign's ``postprocessing_steps`` table held exactly one row while four steps
had demonstrably run and populated their tables.

Observed on campaign basic-nav-gazebo-2026-08-16-20153470: ``_transient/postprocessing.yaml``
listed only ``resource_usage``. The local lane, which passes a provenance file for every
step, was unaffected -- so what a campaign could tell you about its own derivation
depended on which lane it ran on, which is exactly what provenance must not do.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

import json

import pytest

from robovast.results_processing.postprocessing import STAGED_PROVENANCE, _staged_provenance_entries


@pytest.fixture
def campaign(tmp_path):
    (tmp_path / "_execution").mkdir()
    return tmp_path


def _stage(campaign, payload):
    path = campaign / STAGED_PROVENANCE
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload,
                    encoding="utf-8")


def test_entries_written_by_another_stage_are_read(campaign):
    _stage(campaign, {"entries": [
        {"output": "cfg/0/poses.csv", "sources": ["cfg/0/rosbag2"],
         "plugin": "rosbags_process/tf_to_csv", "params": {"frames": "all"}},
    ]})

    entries = _staged_provenance_entries(str(campaign))

    assert len(entries) == 1
    assert entries[0]["plugin"] == "rosbags_process/tf_to_csv"
    assert entries[0]["output"] == "cfg/0/poses.csv"


def test_no_staged_file_is_the_normal_case(campaign):
    """The local lane records everything inline, so absence is not a problem."""
    assert _staged_provenance_entries(str(campaign)) == []


def test_a_malformed_file_is_logged_not_raised(campaign, caplog):
    """Provenance describes work that already succeeded. Failing the campaign because
    its *description* could not be parsed would turn a complete result into a failed
    one."""
    _stage(campaign, "{not json")

    assert _staged_provenance_entries(str(campaign)) == []
    assert any("staged provenance" in r.message.lower() or "staged provenance" in r.msg.lower()
               for r in caplog.records), "the unreadable file must be reported somewhere"


def test_non_dict_entries_are_dropped(campaign):
    """A list of strings is not provenance, and would break the YAML projection
    downstream where it is read with `.get`."""
    _stage(campaign, {"entries": [{"output": "a.csv"}, "nonsense", None, 42]})

    assert _staged_provenance_entries(str(campaign)) == [{"output": "a.csv"}]


def test_the_job_and_the_reader_agree_on_the_path():
    """Two halves, two files, one constant each. They must name the same location, and
    nothing else checks that they do."""
    from robovast.execution.cluster_execution.postprocess_job import _ROSBAG_PROVENANCE_REL

    # Both constants are campaign-relative now: the conversion writes into the shared
    # campaign mount at campaign-relative paths, and the reader resolves the same path
    # against the campaign root. So they are not two paths that have to correspond -- they
    # are one path, and this is what keeps them one.
    assert _ROSBAG_PROVENANCE_REL == STAGED_PROVENANCE


def test_the_job_actually_passes_a_provenance_file():
    """The original bug: the conversion command was assembled without one, so every rosbags_*
    step recorded nothing at all."""
    from robovast.execution.cluster_execution.postprocess_job import (
        CAMPAIGN_MOUNT, _ROSBAG_PROVENANCE_REL, _conversion_script)

    script = _conversion_script([{"plugins": [{"type": "tf_to_csv"}]}], force=False,
                                campaign_id="camp-1")

    assert "--provenance-file" in script, script
    # Under the campaign tree in the pod, which is what `_upload_derived` sends back.
    assert f"{CAMPAIGN_MOUNT}/camp-1/{_ROSBAG_PROVENANCE_REL}" in script, script
