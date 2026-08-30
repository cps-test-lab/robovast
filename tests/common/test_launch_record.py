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

import yaml

from robovast.common.campaign_data import read_launch_record, write_launch_record
from robovast.service.interface import CreateCampaignRequest


def test_the_request_round_trips(tmp_path):
    request = CreateCampaignRequest(
        workspace_id="ws-abc", config_path="p.vast", config_filter="nav-open-space*",
        campaign_name="pilot", runs=1, postprocess=True, upload_to_share=False,
        show_gui=False, description="ignored here")
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


def test_resolved_images_are_recorded(tmp_path):
    """The one field here that is not a request field, and the reason it belongs.

    A campaign is launched against ``build:<tag>``; what actually ran is a concrete ref.
    Recording only the request would leave a re-launch after a service restart to
    re-resolve it — and pick up a base image that moved since, swapping the image under
    half the runs of a campaign already in flight.
    """
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws"),
                        images={"scenario": "reg.example.com/exp@sha256:abc"})

    record = read_launch_record(tmp_path)
    assert record["images"] == {"scenario": "reg.example.com/exp@sha256:abc"}


def test_unresolved_images_are_absent_rather_than_null(tmp_path):
    """The first write happens before the build, so there is nothing to say yet.

    A ``null`` would be indistinguishable from "this campaign resolved no images", which
    is a different statement — and one a re-launch cannot act on.
    """
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws"))
    assert "images" not in read_launch_record(tmp_path)

    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws"), images={})
    assert "images" not in read_launch_record(tmp_path)


def test_the_second_write_adds_images_without_losing_the_request(tmp_path):
    """The launch path writes twice — once at the top of the worker, once after the build."""
    request = CreateCampaignRequest(workspace_id="ws", config_filter="pilot*", runs=3)
    write_launch_record(tmp_path, request)
    write_launch_record(tmp_path, request, images={"scenario": "img@sha256:def"})

    record = read_launch_record(tmp_path)
    assert record["config_filter"] == "pilot*" and record["runs"] == 3
    assert record["images"] == {"scenario": "img@sha256:def"}
