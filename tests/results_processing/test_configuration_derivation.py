# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which authored ``.vast`` configuration a generated cell came from.

Config generation stamps the parent configuration's name onto every cell it expands, and
the metadata pipeline turns that into the public ``derived_from`` -- the only link from a
cell back to a configuration a reader can find in the ``.vast``, and the
``prov:wasDerivedFrom`` edge of the provenance graph.

The phase that reads it runs after the phase that strips internal fields, so a field a
later phase consumes has to be named as one that outlives the strip. Left out, nothing
fails: the public field is simply always empty.
"""

import json

import yaml

from robovast.results_processing.fair_metadata import _as_list
from robovast.results_processing.metadata import generate_campaign_metadata

DATASET_IRI = "https://purl.org/robovast/datasets/test/"

VAST = """\
version: 4
configuration:
- name: cfg
  parameters:
  - speed: 1.0
execution:
  containers: {scenario: {image: img}}
  runs: 1
  scenario_file: scenario.osc
"""


def _campaign(tmp_path, configs):
    """A results directory holding one campaign whose cells are *configs*.

    Each cell gets the one passing run the record builder needs to describe it, so what
    the assertions read is a campaign the real entry point walked end to end.
    """
    root = tmp_path / "camp-2026-09-09-120000"
    (root / "_execution").mkdir(parents=True)
    (root / "_transient").mkdir(parents=True)
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "campaign.vast").write_text(VAST)
    (root / "_transient" / "configurations.yaml").write_text(yaml.safe_dump({
        "_run_files": [],
        "metadata": {"dataset_iri": DATASET_IRI},
        "configs": configs,
        "created_at": "2026-09-09T12:00:00",
    }))
    (root / "_execution" / "execution.yaml").write_text(yaml.safe_dump({
        "runs": 1,
        "execution_type": "cluster",
        "robovast_version": "0000000",
        "execution_time": "2026-09-09T12:00:00",
    }))
    for config in configs:
        run = root / config["name"] / "1"
        run.mkdir(parents=True)
        (run / "test.xml").write_text(
            '<?xml version="1.0"?>'
            '<testsuite tests="1" failures="0" time="1.5" '
            'timestamp="2026-09-09T12:00:00">'
            '<testcase name="scenario" time="1.5"/></testsuite>')
        (run / "sysinfo.yaml").write_text(yaml.safe_dump({"cpu": {"model": "x"}}))
    return root


def _configurations(results_dir, root):
    """Generate through the real entry point and return the record it wrote, by cell."""
    ok, message = generate_campaign_metadata(str(results_dir))
    assert ok, message
    written = yaml.safe_load((root / "metadata.yaml").read_text())
    return {c["name"]: c for c in written["configurations"]}


def test_a_cell_records_the_vast_configuration_it_was_expanded_from(tmp_path):
    root = _campaign(tmp_path, [{"name": "cfg-1", "_config_name": "cfg"}])

    assert _configurations(tmp_path, root)["cfg-1"]["derived_from"] == "cfg"


def test_a_cell_with_no_parent_carries_no_derivation(tmp_path):
    """Absent is not empty: a reader can tell "no parent recorded" from "derived from
    nothing" only if the key is missing rather than falsy."""
    root = _campaign(tmp_path, [{"name": "cfg-1"}])

    assert "derived_from" not in _configurations(tmp_path, root)["cfg-1"]


def _nodes_typed(graph, suffix):
    """Graph nodes of one type, matched by suffix: compaction prefixes a term."""
    return [n for n in graph
            if any(str(t).endswith(suffix) for t in _as_list(n.get("@type", [])))]


def test_the_provenance_graph_joins_the_cell_to_that_configurations_node(tmp_path):
    """The edge has to name the IRI the graph gives that configuration, or it points at
    nothing -- a literal beside the node it was supposed to reach."""
    root = _campaign(tmp_path, [{"name": "cfg-1", "_config_name": "cfg"}])
    _configurations(tmp_path, root)

    graph = json.loads((root / "metadata.prov.json").read_text())["@graph"]
    concrete = _nodes_typed(graph, "ConcreteScenario")
    logical = _nodes_typed(graph, "LogicalScenario")

    assert len(concrete) == 1 and len(logical) == 1
    assert _as_list(concrete[0]["wasDerivedFrom"]) == [logical[0]["@id"]]


def test_a_configuration_that_varies_nothing_is_still_a_node(tmp_path):
    """The ``.vast`` declaring it is what puts it in the graph. Its cell's derivation and
    the ``.vast`` collection's membership both name that node, so a configuration without
    variations may not be missing from it."""
    root = _campaign(tmp_path, [{"name": "cfg-1", "_config_name": "cfg"}])
    _configurations(tmp_path, root)

    graph = json.loads((root / "metadata.prov.json").read_text())["@graph"]
    logical = _nodes_typed(graph, "LogicalScenario")
    collection = _nodes_typed(graph, "VastConfiguration")

    assert [n["@id"] for n in logical] == [f"dataset:{root.name}/cfg"]
    assert _as_list(collection[0]["hadMember"]) == [logical[0]["@id"]]
