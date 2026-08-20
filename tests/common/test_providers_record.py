# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``_execution/providers.yaml`` — which distributions supplied a campaign's assets.

Derived in postprocessing from what the containers recorded, and that is the whole point of
this file. The question is "which installed distributions register a provider group", and only
a container can answer it: the packages are in its image and nowhere else. It used to be
answered by walking the interpreter of whatever process prepared the campaign, which was right
on a local lane (roqsim is installed beside the service) and empty on a cluster one (the service
pod carries no simulator) -- so a campaign that used three private providers recorded none, and
the publication gate certified it as depending on nothing.

The record has THREE states and the distinction is what makes it worth writing. Populated is
"these providers"; empty is "asked, and there were none"; absent is "could not ask", which
``read_providers_record`` documents as unknown and the gate classifies as opaque. A false clean
is worse than a refusal, because a refusal gets examined.
"""

import json

import pytest
import yaml

from robovast.common.campaign_data import read_providers_record
from robovast.common.config_plugins import providers_from_records
from robovast.results_processing.postprocessing import _record_campaign_providers

GROUPS = ("roqsim.models", "roqsim.worlds", "roqsim.plugins")

# What a container writes: every distribution it holds, whether or not it is a provider.
SIM_RECORD = {
    "roqsim_assets_props": {
        "version": "0.1.0", "groups": ["roqsim.models"],
        "direct_url": {"url": "https://host/private-assets",
                       "vcs_info": {"commit_id": "c" * 40, "vcs": "git"},
                       "subdirectory": "roqsim_assets_props"}},
    "roqsim_scenes": {
        "version": "0.1.0", "groups": ["roqsim.worlds"],
        "direct_url": {"url": "file:///opt/roqsim/roqsim_scenes", "dir_info": {}}},
    "numpy": {"version": "1.26.4", "groups": []},
}
SUT_RECORD = {"numpy": {"version": "1.26.4", "groups": []}}


def _campaign(tmp_path, *, records=(), vast: str = "", batch: bool = True):
    """A campaign directory shaped like a real one: job artifacts under
    ``_jobs/[<batch>/]job-N/`` and the frozen ``.vast`` under ``_config/``."""
    root = tmp_path / "camp-1"
    for index, record in enumerate(records):
        parts = ["_jobs"] + (["batch-0"] if batch else []) + [f"job-{index}"]
        job = root.joinpath(*parts)
        job.mkdir(parents=True, exist_ok=True)
        name = "main" if index == 0 else f"sidecar{index}"
        (job / f"distributions_{name}.json").write_text(json.dumps(record), encoding="utf-8")
    if vast:
        config = root / "_config"
        config.mkdir(parents=True, exist_ok=True)
        (config / "camp.vast").write_text(vast, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sink():
    lines = []
    return lines, lines.append


# ---------------------------------------------------------------------------
# the union, which is what a campaign-level record means
# ---------------------------------------------------------------------------

def test_providers_are_unioned_across_containers():
    """Providers differ per role -- a sut image has none, the simulation image has them all,
    and in the ROS shape they are not even the same image. The question the record answers is
    campaign-level, so a provider any container held belongs in it."""
    out = providers_from_records([SUT_RECORD, SIM_RECORD], GROUPS)
    assert sorted(out) == ["roqsim_assets_props", "roqsim_scenes"]


def test_a_non_provider_is_not_a_provider():
    """Every distribution is recorded by the container; only those registering one of the
    simulator's groups are providers. numpy is in every image and belongs in no such record."""
    assert "numpy" not in providers_from_records([SIM_RECORD], GROUPS)


def test_a_vcs_install_is_recorded_as_obtainable():
    """The difference the publication gate turns on: a private provider WITH a commit is
    reproducible by anyone who has access, where a version alone is not."""
    entry = providers_from_records([SIM_RECORD], GROUPS)["roqsim_assets_props"]
    assert entry["commit"] == "c" * 40
    assert entry["url"] == "https://host/private-assets"


def test_a_directory_install_has_no_commit_to_record():
    """A provider baked into an image from a path -- which is what the retired prebuilt image
    did to all of them, and why a dataset built on it could not be published."""
    entry = providers_from_records([SIM_RECORD], GROUPS)["roqsim_scenes"]
    assert "commit" not in entry and entry["url"].startswith("file://")


def test_no_groups_means_no_filter_rather_than_no_providers():
    """An unresolvable backend cannot yield an empty record: that would claim the campaign used
    nothing, where the truth is that nobody could ask."""
    assert providers_from_records([SIM_RECORD], ()) == {}


# ---------------------------------------------------------------------------
# the three states, through the writer
# ---------------------------------------------------------------------------

_VAST = """
version: 2
configuration:
- name: probe
execution:
  mode: base
  containers:
    simulation:
      backend: roqsim
      config: roqsim_scenes:depot
  scenario_file: probe.osc
  runs: 1
"""


@pytest.mark.parametrize("batch", [True, False], ids=["cluster-layout", "local-layout"])
def test_the_record_is_found_in_either_lane_s_job_layout(tmp_path, batch):
    """``_jobs/[<batch>/]job-N/`` -- the batch level exists on the cluster lane and not on the
    local one. A reader assuming either shape finds nothing on the other, which is the failure
    mode this whole record was written to escape."""
    root = _campaign(tmp_path, records=[SIM_RECORD], vast=_VAST, batch=batch)
    lines, output = _sink()
    _record_campaign_providers(root, output)
    record = read_providers_record(root)
    assert record and "roqsim_assets_props" in record, lines


def test_no_container_record_leaves_the_answer_unknown(tmp_path):
    """A campaign whose runs never started, or predate the record. Absent, not empty."""
    root = _campaign(tmp_path, records=[], vast=_VAST)
    lines, output = _sink()
    _record_campaign_providers(root, output)
    assert read_providers_record(root) is None
    assert any("leaving the record absent" in line for line in lines)


def test_an_unresolvable_backend_leaves_the_answer_unknown(tmp_path):
    """Records but no groups to filter by: the question could not be put."""
    root = _campaign(tmp_path, records=[SIM_RECORD], vast="version: 2\nexecution: {}\n")
    lines, output = _sink()
    _record_campaign_providers(root, output)
    assert read_providers_record(root) is None


def test_a_campaign_with_providers_records_them_on_disk(tmp_path):
    root = _campaign(tmp_path, records=[SUT_RECORD, SIM_RECORD], vast=_VAST)
    _record_campaign_providers(root, lambda _m: None)
    on_disk = yaml.safe_load((root / "_execution" / "providers.yaml").read_text())
    assert on_disk["roqsim_assets_props"]["commit"] == "c" * 40


def test_recording_never_fails_postprocessing(tmp_path):
    """Provenance about a campaign must not become the reason its postprocessing fails."""
    lines, output = _sink()
    _record_campaign_providers(tmp_path / "does-not-exist", output)
    assert lines  # it said something rather than raising


def test_unknown_is_opaque_and_empty_is_clean(tmp_path):
    """The distinction, asserted through the code that consumes it."""
    from robovast.results_processing.reproducibility import _classify_providers

    assert [e["class"] for e in _classify_providers(None)] == ["opaque"]
    assert _classify_providers({}) == []
