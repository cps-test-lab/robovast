# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign that ended before running every run it planned can still describe itself.

The record builder holds every configuration to the campaign's planned run count, because
in a campaign that ran to the end a missing run directory is lost data. A campaign that was
stopped, failed or crashed holds fewer runs than it planned by construction. Its outcome
record says how it ended before it is postprocessed -- the service writes ``stopped`` and
then postprocesses the batches that finished -- so the record says that too, instead of
refusing to exist while the derived data of the runs that did finish is complete.
"""

import pytest
import yaml

from robovast.client.status import Status
from robovast.common.campaign_data import write_execution_outcome
from robovast.results_processing.metadata import MetadataGenerator, generate_campaign_metadata

VAST = """\
version: 4
configuration:
- name: cfg
  parameters:
  - speed: 1.0
execution:
  containers: {scenario: {image: img}}
  runs: 3
  scenario_file: scenario.osc
"""


def _campaign(tmp_path, runs_by_config, *, planned=3, phase="stopped"):
    """A campaign planned at *planned* runs per configuration, holding the runs
    *runs_by_config* names, whose outcome record ended in *phase* (``None``: no record)."""
    root = tmp_path / "camp-2026-09-21-120000"
    (root / "_execution").mkdir(parents=True)
    (root / "_transient").mkdir(parents=True)
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "campaign.vast").write_text(VAST)
    (root / "_transient" / "configurations.yaml").write_text(yaml.safe_dump({
        "_run_files": [], "metadata": {"name": "camp"},
        "configs": [{"name": name} for name in runs_by_config],
        "created_at": "2026-09-21T12:00:00"}))
    (root / "_execution" / "execution.yaml").write_text(yaml.safe_dump({
        "runs": planned, "execution_type": "cluster", "robovast_version": "0000000",
        "execution_time": "2026-09-21T12:00:00"}))
    if phase is not None:
        write_execution_outcome(root, Status(phase=phase))
    for name, count in runs_by_config.items():
        (root / name).mkdir()
        for number in range(count):
            run = root / name / str(number)
            run.mkdir()
            (run / "test.xml").write_text(
                '<?xml version="1.0"?><testsuite tests="1" failures="0" time="1.5" '
                'timestamp="2026-09-21T12:00:00"><testcase name="scenario" time="1.5"/>'
                '</testsuite>')
            (run / "sysinfo.yaml").write_text(yaml.safe_dump({"cpu": {"model": "x"}}))
    return root


@pytest.mark.parametrize("phase", ["stopped", "failed", "crashed"])
def test_the_record_says_how_the_campaign_ended_and_what_it_holds(tmp_path, phase):
    root = _campaign(tmp_path, {"cfg-a": 3, "cfg-b": 1, "cfg-c": 0}, phase=phase)

    metadata = MetadataGenerator(root).generate_metadata()

    assert metadata["execution"]["ended_early"] == phase
    assert metadata["execution"]["runs"] == 3, "the plan is still the plan"
    configs = {c["name"]: c for c in metadata["configurations"]}
    assert {n: len(c["test_results"]) for n, c in configs.items()} == {
        "cfg-a": 3, "cfg-b": 1, "cfg-c": 0}
    assert [r["dir"] for r in configs["cfg-b"]["test_results"]] == ["cfg-b/0"]


def test_the_whole_pipeline_writes_the_record_of_a_stopped_campaign(tmp_path):
    """Through the entry the postprocessing host calls, so every phase after the record
    builder -- variation hooks, user processors, derivation, the provenance graph -- also
    meets a campaign whose configurations are short."""
    root = _campaign(tmp_path, {"cfg-a": 3, "cfg-b": 1, "cfg-c": 0})

    ok, message = generate_campaign_metadata(str(tmp_path))

    assert ok, message
    written = yaml.safe_load((root / "metadata.yaml").read_text())
    assert written["execution"]["ended_early"] == "stopped"
    # The provenance graph is non-fatal, so only its file says it was built.
    assert (root / "metadata.prov.json").exists()


@pytest.mark.parametrize("phase", ["finished", None])
def test_a_campaign_with_no_record_of_ending_early_is_held_to_its_plan(tmp_path, phase):
    """A missing run directory in a campaign that says it ran to the end -- or says nothing,
    which is every campaign while its own postprocessing runs -- is lost data."""
    root = _campaign(tmp_path, {"cfg-a": 3, "cfg-b": 1}, phase=phase)

    with pytest.raises(ValueError, match="has 1 run directories but expected 3 runs"):
        MetadataGenerator(root).generate_metadata()


def test_a_finished_campaign_carries_no_ended_early_keys(tmp_path):
    root = _campaign(tmp_path, {"cfg-a": 3}, phase="finished")

    metadata = MetadataGenerator(root).generate_metadata()

    assert "ended_early" not in metadata["execution"]


def test_more_runs_than_planned_is_never_explained_by_ending_early(tmp_path):
    root = _campaign(tmp_path, {"cfg-a": 4}, phase="stopped")

    with pytest.raises(ValueError, match="has 4 run directories but expected 3 runs"):
        MetadataGenerator(root).generate_metadata()


@pytest.mark.parametrize("phase", ["stopped", "failed", "crashed"])
def test_a_campaign_that_ended_before_any_verdict_is_still_described(tmp_path, phase):
    """An ending cuts runs short before they reach a verdict, so a campaign stopped during
    its only wave has runs and not one verdict -- the shape the ending leaves, not a broken
    input."""
    root = _campaign(tmp_path, {"cfg-a": 2}, phase=phase)
    for run in (root / "cfg-a").iterdir():
        (run / "test.xml").unlink()

    metadata = MetadataGenerator(root).generate_metadata()

    assert metadata["execution"]["ended_early"] == phase
    assert metadata["runs_without_verdict"] == ["cfg-a/0", "cfg-a/1"]


def test_a_campaign_that_ran_to_the_end_with_no_verdict_is_still_refused(tmp_path):
    root = _campaign(tmp_path, {"cfg-a": 3}, phase="finished")
    for run in (root / "cfg-a").iterdir():
        (run / "test.xml").unlink()

    with pytest.raises(ValueError, match="no run of this campaign recorded a verdict"):
        MetadataGenerator(root).generate_metadata()


def test_an_outcome_record_that_cannot_be_read_is_an_error_not_silence(tmp_path):
    """A failed read is not a campaign that said nothing: reporting "no record of ending
    early" for a record that exists and is broken would name the wrong cause."""
    root = _campaign(tmp_path, {"cfg-a": 1})
    (root / "_execution" / "outcome.json").write_text("{not json")

    with pytest.raises(Exception, match="(?i)json"):
        MetadataGenerator(root).generate_metadata()
