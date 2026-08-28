# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""PROV-O provenance graph generation from campaign metadata.

The graph is generated best-effort -- a failure warns rather than discarding the runs --
which is why the shapes an author may write have to be accepted here rather than caught
downstream. A single ``derived_from: <iri>``, the form every .vast in practice uses, was
iterated character by character and lost the whole graph to an AttributeError on ``str``.
"""

import json

import pytest

from robovast.results_processing.fair_metadata import _as_list, generate_prov_metadata


def _metadata(agents):
    """The shape metadata.yaml has, reduced to what the graph builder reads."""
    return {
        "run_files": ["_config/campaign.vast"],
        "scenario_file": "_config/scenario.osc",
        "metadata": {
            "dataset_iri": "https://purl.org/robovast/datasets/test/",
            "agents": agents,
        },
        "configurations": [],
        "execution": {
            "execution_time": "2026-08-10T05:17:00.931964+00:00",
            "robovast_version": "8e130a9",
            "runs": 1,
            "execution_type": "cluster",
        },
        "postprocessing": {"generated_by": "robovast", "entries": []},
    }


def _graph(campaign_dir):
    nodes = json.loads((campaign_dir / "metadata.prov.json").read_text())
    return nodes.get("@graph", nodes)


def _derived(campaign_dir, agent_id):
    """An agent's wasDerivedFrom IRIs, always as a list.

    JSON-LD compaction renders a single-valued property as a scalar and a multi-valued
    one as an array, so the shape here follows the count rather than the input.
    """
    for node in _graph(campaign_dir):
        # By type, not by id alone: the derived_from target is itself a node, and an IRI
        # ending in the agent's name would otherwise be picked up instead of the agent.
        if ("SoftwareAgent" in _as_list(node.get("@type", []))
                and str(node.get("@id", "")).endswith(f"/{agent_id}")):
            return _as_list(node.get("wasDerivedFrom", []))
    return None


VAST = """\
version: 3
configuration:
- name: ca
  parameters:
  - speed: 1.0
execution:
  containers: {scenario: {image: img}}
  runs: 1
  scenario_file: scenario.osc
"""


@pytest.fixture
def campaign(tmp_path):
    root = tmp_path / "campaign-2026-08-10-07150919"
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "campaign.vast").write_text(VAST)
    return root


@pytest.mark.parametrize("written, expected", [
    ("https://example.org/tb4", ["https://example.org/tb4"]),
    (["https://example.org/tb4"], ["https://example.org/tb4"]),
    (["https://example.org/a", "https://example.org/b"],
     ["https://example.org/a", "https://example.org/b"]),
    ([{"source": "https://example.org/tb4"}], ["https://example.org/tb4"]),
    ({"source": "https://example.org/tb4"}, ["https://example.org/tb4"]),
])
def test_derived_from_accepts_one_or_many_iris(campaign, written, expected):
    """One IRI, a list of them, or a mapping -- all are things an author writes."""
    ok, message = generate_prov_metadata(
        campaign, _metadata([{"id": "tb4", "derived_from": written}]),
        generate_visualization=False)
    assert ok, message
    assert _derived(campaign, "tb4") == expected


def test_a_versioned_source_keeps_its_version(campaign):
    ok, _ = generate_prov_metadata(
        campaign,
        _metadata([{"id": "tb4", "derived_from": [
            {"source": "https://example.org/tb4", "version": "1.2.3"}]}]),
        generate_visualization=False)
    assert ok
    versions = [n.get("hasVersion") for n in _graph(campaign)
                if n.get("@id") == "https://example.org/tb4"]
    assert versions == ["1.2.3"]


def test_an_agent_without_derived_from_still_gets_a_node(campaign):
    ok, _ = generate_prov_metadata(campaign, _metadata([{"id": "tb4", "type": "robot"}]),
                                   generate_visualization=False)
    assert ok
    assert _derived(campaign, "tb4") == []


def test_an_unusable_derived_from_entry_is_skipped_not_fatal(campaign, caplog):
    """One malformed entry costs its own node, not the campaign's whole provenance."""
    ok, _ = generate_prov_metadata(
        campaign,
        _metadata([{"id": "tb4", "derived_from": [
            "https://example.org/good", {"version": "1.0"}, 42]}]),
        generate_visualization=False)
    assert ok
    assert _derived(campaign, "tb4") == ["https://example.org/good"]
    assert "tb4" in caplog.text


def test_a_single_configuration_file_is_not_matched_character_by_character(campaign, caplog):
    ok, _ = generate_prov_metadata(
        campaign, _metadata([{"id": "tb4", "configuration_files": "files/nav2.yaml"}]),
        generate_visualization=False)
    assert ok
    # The file is absent from run_files here, so it warns once -- for the path, not for
    # each of its characters.
    assert caplog.text.count("not found in run_files") == 1


def test_a_campaign_with_no_agents_still_produces_a_graph(campaign):
    ok, _ = generate_prov_metadata(campaign, _metadata([]), generate_visualization=False)
    assert ok
    assert _graph(campaign)


@pytest.mark.parametrize("value, expected", [
    (None, []),
    ("one", ["one"]),
    (["one", "two"], ["one", "two"]),
    ({"source": "x"}, [{"source": "x"}]),
    ((1, 2), [1, 2]),
])
def test_as_list_wraps_a_scalar_and_leaves_a_sequence(value, expected):
    assert _as_list(value) == expected

def test_a_failed_contribution_is_recorded_in_the_graph(campaign, caplog, monkeypatch):
    """A provenance graph must declare its own gaps.

    Logging the hook's failure and skipping it leaves a *published* record whose
    incompleteness is invisible: nothing distinguishes a variation whose contribution failed
    from one that had nothing to contribute. Warning kept, gap added -- in the artifact, and
    in the message the postprocessing step reports.
    """
    class Exploding:
        @classmethod
        def collect_prov_metadata(cls, **kwargs):
            raise RuntimeError("map file went missing")

    from robovast.results_processing import fair_metadata as fm
    monkeypatch.setattr(fm, "load_variation_classes", lambda: {"Exploding": Exploding})

    (campaign / "ca").mkdir()
    metadata = _metadata([])
    metadata["configurations"] = [{
        "name": "ca",
        "variations": [{"name": "Exploding",
                        "started_at": "2026-08-10T05:17:00.931964+00:00",
                        "duration": 1.0}],
    }]

    ok, message = generate_prov_metadata(campaign, metadata, generate_visualization=False)

    assert ok, message
    assert "Exploding in ca" in message and "map file went missing" in message
    gaps = [n for n in _graph(campaign)
            if "ProvenanceGap" in json.dumps(n.get("@type", n.get("type", "")))]
    assert gaps, "the graph does not declare the contribution it is missing"
    assert "Exploding in ca" in json.dumps(gaps[0])
    assert "collect_prov_metadata failed" in caplog.text
