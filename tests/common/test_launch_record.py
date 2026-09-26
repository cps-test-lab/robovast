# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``_execution/launch.yaml`` — how a campaign was ASKED FOR.

A campaign recorded plenty about what happened to it and nothing about what was requested.
``config_filter`` in particular lived only in the request and was consumed inside
``build_campaign_data``, so "was this the full sweep or a one-config pilot?" could not be
answered about any campaign in the results root — not by a person, and not by a retrigger,
which would therefore have turned a piloted campaign into a full sweep.

The other half is ``runs``: ``execution.yaml`` records the *effective* count, so "3 because the
.vast says 3" and "3 because someone overrode a .vast saying 25" were indistinguishable. The
pair answers it; neither number does alone.
"""

import pytest
import yaml

from robovast.common.campaign_data import (LaunchImages, read_launch_record,
                                           update_launch_images, update_launch_scheduling,
                                           write_launch_record)
from robovast.service.interface import CreateCampaignRequest


def test_the_request_round_trips(tmp_path):
    request = CreateCampaignRequest(
        workspace_id="ws-abc", config_path="p.vast", config_filter="nav-open-space*",
        campaign_name="pilot", runs=1, postprocess=True, upload_to_share=False,
        description="ignored here")
    write_launch_record(tmp_path, request)

    record = read_launch_record(tmp_path)
    assert record["config_filter"] == "nav-open-space*"
    assert record["runs"] == 1
    assert record["campaign_name"] == "pilot"


def test_the_workspace_binding_is_not_recorded(tmp_path):
    """Campaigns are workspace-independent; recording the binding would preserve a link that
    means nothing once the campaign exists — and the workspace may be gone by then."""
    write_launch_record(tmp_path, CreateCampaignRequest(
        workspace_id="ws-abc", config_path="sub/p.vast"))
    record = read_launch_record(tmp_path)
    assert "workspace_id" not in record and "config_path" not in record


def test_runs_is_stored_as_requested_not_as_resolved(tmp_path):
    """``0`` has to survive as ``0``: it means "take the .vast's ``execution.runs``", and
    resolving it here would erase the very distinction the record exists to make."""
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="w", runs=0))
    assert read_launch_record(tmp_path)["runs"] == 0


def test_it_lands_beside_the_other_execution_records(tmp_path):
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="w"))
    assert (tmp_path / "_execution" / "launch.yaml").is_file()


def test_a_campaign_without_one_reads_as_none(tmp_path):
    """Campaigns predating this file are not an error — each reader decides what to do about
    the field it wanted (the retrigger falls back to ``execution.yaml``'s effective runs)."""
    assert read_launch_record(tmp_path) is None


def test_a_blank_record_reads_as_none(tmp_path):
    """An empty file is indistinguishable from absent for every field a caller reads, so it
    must not come back as ``{}`` for ``.get`` to silently return ``None`` from."""
    (tmp_path / "_execution").mkdir()
    (tmp_path / "_execution" / "launch.yaml").write_text("")
    assert read_launch_record(tmp_path) is None


def test_the_metadata_document_carries_it_under_execution(tmp_path):
    """Three files exist only because they can be written at three different times; nobody
    reading a published campaign should have to know that."""
    from robovast.results_processing.metadata import MetadataGenerator
    (tmp_path / "_execution").mkdir()
    (tmp_path / "_execution" / "execution.yaml").write_text(
        yaml.safe_dump({"runs": 3, "execution_type": "local"}))
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="w", runs=0,
                                                       config_filter="config1*"))
    (tmp_path / "_transient").mkdir()
    (tmp_path / "_transient" / "configurations.yaml").write_text(
        yaml.safe_dump({"configs": [], "metadata": {}}))

    metadata = MetadataGenerator(tmp_path).generate_metadata()
    # The pair, in one document: requested 0 (i.e. "the .vast's"), effective 3.
    assert metadata["execution"]["launch"]["config_filter"] == "config1*"
    assert metadata["execution"]["launch"]["runs"] == 0
    assert metadata["execution"]["runs"] == 3


SCENARIO = "reg.example.com/exp@sha256:" + "a" * 64
SIM = "reg.example.com/roqsim@sha256:" + "1" * 64
SUT = "reg.example.com/sut@sha256:" + "2" * 64
SIDECAR = "reg.example.com/robovast-sidecar@sha256:" + "3" * 64
AUX = "reg.example.com/robovast-roqsim@sha256:" + "4" * 64


def test_a_replay_states_every_digest_from_the_first_write(tmp_path):
    """The one set of fields here that is not a request field, and the reason it belongs.

    A replay -- a retrigger, an adoption after a restart -- runs the digests it replays, and
    the record says so from the moment the campaign exists: a replay cut short before its
    first pod still names every image it was going to run.
    """
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws"),
                        images=LaunchImages(containers={"scenario": SCENARIO},
                                            sidecar=SIDECAR, aux={"aux-robovast-roqsim": AUX}))

    record = read_launch_record(tmp_path)
    assert record["images"] == {"scenario": SCENARIO}
    assert record["sidecar_image"] == SIDECAR
    assert record["aux_images"] == {"aux-robovast-roqsim": AUX}


def test_images_not_fixed_yet_are_absent_rather_than_null(tmp_path):
    """A fresh launch writes the record before any image is fixed, so there is nothing to say.

    A ``null`` would be indistinguishable from "this campaign runs no such image", which is a
    different statement -- and one a replay cannot act on.
    """
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws"))
    record = read_launch_record(tmp_path)
    assert not {"images", "sidecar_image", "aux_images"} & set(record)

    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws"),
                        images=LaunchImages())
    assert not {"images", "sidecar_image", "aux_images"} & set(read_launch_record(tmp_path))


def _request():
    return CreateCampaignRequest(
        workspace_id="ws-abc", config_path="p.vast", config_filter="goal-1",
        campaign_name="pilot", runs=1, postprocess=True, upload_to_share=False,
        description="")


def test_each_image_is_merged_in_as_it_is_fixed(tmp_path):
    """The sidecar as the span begins, a helper image when composition asks for it, the
    containers before the first batch -- three moments, one record, nothing lost between."""
    write_launch_record(tmp_path, _request())

    update_launch_images(tmp_path, sidecar=SIDECAR)
    update_launch_images(tmp_path, aux={"aux-robovast-roqsim": AUX})
    update_launch_images(tmp_path, containers={"simulation": SIM})
    update_launch_images(tmp_path, containers={"sut": SUT})

    record = read_launch_record(tmp_path)
    assert record["images"] == {"simulation": SIM, "sut": SUT}
    assert record["sidecar_image"] == SIDECAR
    assert record["aux_images"] == {"aux-robovast-roqsim": AUX}
    # Merged, not replaced: the caller knows its images, not the request beside them.
    assert record["config_filter"] == "goal-1"


def test_a_tag_is_refused_by_the_record(tmp_path):
    """The record is what every replay runs from, so a tag in it would be resolved again by
    the replay it exists to prevent. Refused where it is written, naming it."""
    write_launch_record(tmp_path, _request())

    for kwargs in ({"containers": {"sut": "reg.example.com/sut:latest"}},
                   {"sidecar": "reg.example.com/robovast-sidecar:latest"},
                   {"aux": {"aux-tool": "reg.example.com/tool:1"}}):
        with pytest.raises(ValueError, match="digests only"):
            update_launch_images(tmp_path, **kwargs)

    assert not {"images", "sidecar_image", "aux_images"} & set(read_launch_record(tmp_path))


def test_a_campaign_with_no_launch_record_does_not_gain_a_bare_one(tmp_path):
    """A record holding images and no request reads as a campaign that asked for nothing --
    worse than the absence, which every reader already handles."""
    update_launch_images(tmp_path, containers={"sut": SUT}, sidecar=SIDECAR)

    assert read_launch_record(tmp_path) is None


def test_recording_nothing_leaves_the_record_alone(tmp_path):
    """A call that fixed nothing must not blank the images already there."""
    write_launch_record(tmp_path, _request(), images=LaunchImages(containers={"sut": SUT}))

    update_launch_images(tmp_path)
    update_launch_images(tmp_path, containers={})

    assert read_launch_record(tmp_path)["images"] == {"sut": SUT}


# -- scheduling ---------------------------------------------------------------------------
#
# The queue holds the live answer in memory. A restart adopts a campaign by re-launching it
# from this file, so a rank or a hold missing here silently reverts.

def test_the_scheduling_is_recorded(tmp_path):
    write_launch_record(tmp_path, CreateCampaignRequest(
        workspace_id="ws-abc", priority=-5, paused=True))
    record = read_launch_record(tmp_path)
    assert record["priority"] == -5
    assert record["paused"] is True


def test_an_unasked_campaign_records_the_defaults(tmp_path):
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws-abc"))
    record = read_launch_record(tmp_path)
    assert record["priority"] == 0
    assert record["paused"] is False


def test_a_changed_rank_is_written_back(tmp_path):
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws-abc"))
    assert update_launch_scheduling(tmp_path, priority=-3) is True
    assert read_launch_record(tmp_path)["priority"] == -3


def test_holding_a_campaign_leaves_the_rank_it_resumes_at(tmp_path):
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws-abc", priority=4))
    update_launch_scheduling(tmp_path, paused=True)
    record = read_launch_record(tmp_path)
    assert record["paused"] is True and record["priority"] == 4


def test_rewriting_keeps_the_rest_of_the_record(tmp_path):
    write_launch_record(tmp_path, CreateCampaignRequest(
        workspace_id="ws-abc", config_filter="nav-*", runs=7),
        images=LaunchImages(containers={"scenario": SCENARIO}, sidecar=SIDECAR))
    update_launch_scheduling(tmp_path, priority=2)
    record = read_launch_record(tmp_path)
    assert record["config_filter"] == "nav-*" and record["runs"] == 7
    assert record["images"] == {"scenario": SCENARIO}
    assert record["sidecar_image"] == SIDECAR


def test_a_campaign_with_no_record_is_left_alone(tmp_path):
    """Writing a bare record here would produce a launch record with no request in it."""
    assert update_launch_scheduling(tmp_path, priority=1) is False
    assert read_launch_record(tmp_path) is None
