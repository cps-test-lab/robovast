# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run that produced no verdict must not stop the campaign describing itself.

A container can start, burn CPU and be recorded in ``resource_usage.csv`` without the
scenario ever reaching a result. Such a run has no ``test.xml`` and no ``sysinfo.yaml``, and
campaigns routinely carry some -- the store counts them as no-result runs and the index
records a container failure per one.

The record builder treated either absence as a broken input and raised, and a metadata
failure fails the postprocess. So one no-result run made a campaign permanently unable to
record that its derived data is complete: observed on a campaign whose conversion finished,
whose index held 68 million rows across 21 tables, and which still could not be marked
postprocessed because one of its 1445 runs never wrote a verdict.

What must still fail is a campaign where nothing can be described, and a run that DID reach
a verdict but recorded no machine -- that is a real gap in the campaign rather than a run
that never got there.
"""

import pytest
import yaml

from robovast.results_processing.metadata import MetadataGenerator


def _campaign(tmp_path, runs):
    """A campaign of one configuration whose runs are described by *runs*.

    Each entry is ``(run_number, has_verdict)``; a run without a verdict gets only the
    files postprocessing derives, which is exactly what such a directory holds.
    """
    root = tmp_path / "camp-2026-09-02-120000"
    config = root / "cfg"
    (root / "_execution").mkdir(parents=True)
    (root / "_transient").mkdir(parents=True)
    (root / "_transient" / "configurations.yaml").write_text(yaml.safe_dump({
        "_run_files": [], "metadata": {"name": "camp"},
        "configs": [{"name": "cfg"}], "created_at": "2026-09-02T12:00:00"}))
    (root / "_execution" / "execution.yaml").write_text(yaml.safe_dump({
        "runs": len(runs), "execution_type": "cluster"}))
    for number, has_verdict in runs:
        run = config / str(number)
        run.mkdir(parents=True)
        (run / "resource_usage.csv").write_text("t,cpu\n0,1\n")
        (run / "run_log.csv").write_text("t,msg\n0,started\n")
        if has_verdict:
            (run / "test.xml").write_text(
                '<?xml version="1.0"?>'
                '<testsuite tests="1" failures="0" time="1.5" '
                'timestamp="2026-09-02T12:00:00">'
                '<testcase name="scenario" time="1.5"/></testsuite>')
            (run / "sysinfo.yaml").write_text(yaml.safe_dump({"cpu": {"model": "x"}}))
    return root


def _generate(root, _runs_declared=None):
    """Through the real entry point, so the walk under test is the one that runs."""
    return MetadataGenerator(root).generate_metadata()


def test_one_run_without_a_verdict_does_not_stop_the_record(tmp_path):
    root = _campaign(tmp_path, [(1, True), (2, False), (3, True)])

    metadata = _generate(root, 3)

    results = {e["dir"]: e for e in metadata["configurations"][0]["test_results"]}
    assert results["cfg/2"]["success"] == "unknown", (
        "no verdict is not the same as a failed verdict")
    assert "no_verdict_reason" in results["cfg/2"]
    assert results["cfg/1"]["success"] == "true"
    # Named at the top level too, so a reader sees the shape of the campaign at a glance.
    assert metadata["runs_without_verdict"] == ["cfg/2"]


def test_a_campaign_where_nothing_can_be_described_still_fails(tmp_path):
    """The line between "a campaign with some failed runs" and a broken input."""
    root = _campaign(tmp_path, [(1, False), (2, False)])

    with pytest.raises(ValueError) as excinfo:
        _generate(root, 2)

    assert "no run of this campaign recorded a verdict" in str(excinfo.value)


def test_a_run_that_reached_a_verdict_still_needs_its_machine_recorded(tmp_path):
    """Tolerating a missing sysinfo for every run would hide a real gap. It is excused
    only for a run that never reached a result, which never recorded one either."""
    root = _campaign(tmp_path, [(1, True)])
    (root / "cfg" / "1" / "sysinfo.yaml").unlink()

    with pytest.raises(FileNotFoundError, match="sysinfo.yaml"):
        _generate(root, 1)


def test_a_campaign_with_no_runs_at_all_is_not_reported_as_verdictless(tmp_path):
    """Nothing to describe is not the same as nothing describable: an empty campaign must
    not trip the "nothing recorded a verdict" refusal, which is about runs that exist."""
    root = _campaign(tmp_path, [])

    metadata = _generate(root, 0)

    assert metadata["configurations"][0]["test_results"] == []
    assert "runs_without_verdict" not in metadata
