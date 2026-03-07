# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""PROV-O provenance metadata generation for RoboVAST campaigns.

Generates a JSON-LD provenance graph (``metadata.prov.json``) and an optional
PDF visualisation from the ``metadata.yaml`` written per campaign.  This step
runs automatically after ``metadata.yaml`` is written during
``generate_campaign_metadata``; it does **not** depend on any postprocessing
plugin configuration.

Domain-specific provenance nodes (e.g. map entities for navigation campaigns)
are contributed by variation plugins via the
:meth:`~robovast.common.variation.base_variation.Variation.collect_prov_metadata`
hook.

Requires: ``rdflib`` and ``pyld``.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

import rdflib
from rdflib import Namespace, PROV, DCTERMS
from rdflib.tools.rdf2dot import rdf2dot
from pyld import jsonld

from robovast.common.variation.loader import load_variation_classes

# ---------------------------------------------------------------------------
# Namespace constants (domain-agnostic)
# ---------------------------------------------------------------------------

_DEFAULT_DATASET_IRI = "https://purl.org/robovast/datasets/default/"

SCENARIOS = Namespace("https://secorolab.github.io/metamodels/scenarios/osc/")
ROBOVAST = Namespace("https://purl.org/robovast/metamodels/")

# JSON-LD context helpers
_ID = "@id"
_CONTEXT = "@context"
_TYPE = "@type"

_BASE_CONTEXT = {
    _CONTEXT: [
        "https://secorolab.github.io/metamodels/metadata.json"
    ]
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def load_graph(file_path: str) -> "rdflib.Graph":
    """Load a JSON-LD file into an rdflib Graph with standard namespace bindings."""
    g = rdflib.Graph()
    g.parse(file_path, format="json-ld")
    g.bind("robovast", ROBOVAST)
    g.bind("scenarios", SCENARIOS)
    return g


def _build_iri_context(dataset_iri: str) -> dict:
    """Build the JSON-LD IRI context with the given dataset IRI."""
    return {
        _CONTEXT: {
            "agn": "https://secorolab.github.io/metamodels/agent#",
            "smm": "https://secorolab.github.io/metamodels/scenarios#",
            "env": "https://secorolab.github.io/metamodels/environment#",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
            "prov": "http://www.w3.org/ns/prov#",
            "dct": "http://purl.org/dc/terms/",
            "dataset": dataset_iri,
        }
    }


def _build_agents(
    agents_config: List[dict],
    run_files: list,
    campaign_ns: Namespace,
) -> Tuple[list, list]:
    """Build PROV Agent nodes from the agents configuration.

    Returns:
        Tuple of (agent_nodes, location_nodes) to append to the graph.
    """
    location_iris = [campaign_ns[rf] for rf in run_files]
    location_nodes = [
        {_ID: loc, _TYPE: PROV["Location"]}
        for loc in location_iris
    ]

    agent_nodes = []
    for agent_cfg in agents_config:
        agent_cfg = dict(agent_cfg)
        name = agent_cfg.pop("name", "agent")
        agent_node = {
            _ID: campaign_ns[name],
            _TYPE: PROV["Agent"],
            "atLocation": location_iris,
        }
        # Remaining keys become properties on the agent node
        for k, v in agent_cfg.items():
            agent_node[ROBOVAST[k]] = v
        agent_nodes.append(agent_node)

    return agent_nodes, location_nodes


def generate_prov_metadata(
    campaign_dir: Path,
    metadata: dict,
) -> Tuple[bool, str]:
    """Generate a PROV-O provenance graph from campaign metadata.

    Reads the already-computed *metadata* dict (the same data that was just
    written to ``metadata.yaml``) and produces:

    * ``<campaign_dir>/metadata.prov.json``  -- compact JSON-LD provenance graph
    * ``<campaign_dir>/metadata.pdf``        -- visualisation (requires Graphviz ``dot``)

    Campaign-level configuration is read from ``metadata["metadata"]``:

    * ``dataset_iri`` -- base IRI for the dataset namespace
    * ``agents``      -- list of ``{name, type, ...}`` dicts for PROV Agent nodes
                         (optional; omit for campaigns with no robot/agent)

    Domain-specific provenance nodes are contributed by variation plugins
    via :meth:`Variation.collect_prov_metadata`.

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.
        metadata:     The metadata dict that was written to ``metadata.yaml``.

    Returns:
        Tuple of ``(success, message)``.
    """
    import json  # noqa: PLC0415

    campaign_dir = Path(campaign_dir)
    campaign = campaign_dir.name + "/"

    # --- Read campaign-level configuration from metadata ---
    md_section = dict(metadata.get("metadata", {}))
    dataset_iri = md_section.pop("dataset_iri", _DEFAULT_DATASET_IRI)
    if not dataset_iri.endswith("/"):
        dataset_iri += "/"
    agents_config = md_section.pop("agents", [])

    DATASET = Namespace(dataset_iri)
    CAMPAIGN = Namespace(f"{dataset_iri}{campaign}")
    iri_context = _build_iri_context(dataset_iri)

    # Load variation plugin classes for PROV hooks
    variation_classes = load_variation_classes()

    graph = []

    # --- Software agents ---
    graph.append({
        _ID: "https://purl.org/robovast/",
        _TYPE: PROV["SoftwareAgent"],
        DCTERMS["hasVersion"]: metadata["execution"]["robovast_version"]
    })
    graph.append({
        _ID: "https://purl.org/secorolab/scenery_builder/",
        _TYPE: PROV["SoftwareAgent"],
        DCTERMS["hasVersion"]: metadata["execution"].get("scenery_builder_version")
    })

    # --- Campaign activity and entity ---
    campaign_activity = {
        _ID: DATASET[campaign + "execution/"],
        _TYPE: [PROV["Activity"], ROBOVAST[metadata["execution"]["execution_type"].capitalize()]],
        "started_at": metadata["execution"]["execution_time"],
        "wasAssociatedWith": "https://purl.org/robovast/",
        ROBOVAST["runs"]: metadata["execution"]["runs"]
    }
    graph.append(campaign_activity)

    campaign_entity = {
        _ID: DATASET[campaign],
        _TYPE: PROV["Entity"],
        "wasGeneratedBy": campaign_activity[_ID]
    }
    graph.append(campaign_entity)

    # --- Agent nodes (robots, manipulators, etc.) ---
    agent_nodes, location_nodes = _build_agents(
        agents_config, metadata.get("run_files", []), CAMPAIGN,
    )
    graph.extend(location_nodes)

    # --- Scenario and config generation ---
    scenario_file = metadata.get("scenario_file", "scenario.osc")
    abstract_scenario = {
        _ID: CAMPAIGN[f"_config/{scenario_file}"],
        _TYPE: [PROV["Entity"], SCENARIOS["AbstractScenario"]],
    }
    graph.append(abstract_scenario)

    # Discover the .vast file from the campaign's _config/ directory
    vast_file_name = None
    config_dir = campaign_dir / "_config"
    if config_dir.is_dir():
        vast_files = list(config_dir.glob("*.vast"))
        if vast_files:
            vast_file_name = vast_files[0].name

    vast_config = {
        _ID: CAMPAIGN[f"_config/{vast_file_name or 'config.vast'}"],
        _TYPE: [PROV["Entity"], ROBOVAST["VastConfiguration"]],
        "references": abstract_scenario[_ID]
    }
    graph.append(vast_config)

    gen_activity = {
        _ID: DATASET[campaign + "config_generation"],
        _TYPE: [PROV["Activity"]],
        "used": [vast_config[_ID], abstract_scenario[_ID]],
        "wasInfluencedBy": campaign_activity[_ID]
    }
    graph.append(gen_activity)

    # --- Per-configuration ---
    all_configs = {c["name"]: c for c in metadata["configurations"]}

    config_names = sorted(
        p.name
        for p in campaign_dir.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )

    for config_name in config_names:
        config_path = config_name + "/"
        CONFIG = Namespace(f"{dataset_iri}{campaign}{config_path}")

        config_md = all_configs.get(config_name)
        if config_md is None:
            continue

        # Concrete scenario node
        scenario_node = {
            _ID: CAMPAIGN[config_path],
            _TYPE: [PROV["Entity"], SCENARIOS["ConcreteScenario"]],
            "wasGeneratedBy": gen_activity[_ID],
            PROV["specializationOf"]: abstract_scenario[_ID],
        }

        # Collect domain-specific PROV contributions from variation plugins
        run_used_iris = [scenario_node[_ID]]

        for vdata in config_md.get("variations", []):
            vtype_name = vdata.get("name", "")
            cls = variation_classes.get(vtype_name)
            if cls is None or not hasattr(cls, "collect_prov_metadata"):
                continue

            try:
                contribution = cls.collect_prov_metadata(
                    config_entry=config_md,
                    campaign_namespace=CAMPAIGN,
                    config_namespace=CONFIG,
                    gen_activity_id=gen_activity[_ID],
                )
            except Exception as e:
                logger.warning(
                    "Variation '%s' collect_prov_metadata failed for '%s': %s",
                    vtype_name, config_name, e,
                )
                continue

            if contribution is None:
                continue

            scenario_node.update(contribution.scenario_properties)
            graph.extend(contribution.graph_nodes)
            run_used_iris.extend(contribution.run_used_iris)

        graph.append(scenario_node)

        # Per-run activities
        for run in config_md.get("test_results", []):
            sysinfo = dict(run.get("sysinfo", {}))
            platform = sysinfo.pop("platform", {})
            sys_info = dict(**platform, **sysinfo)
            sys_info = {ROBOVAST[k]: v for k, v in sys_info.items()}
            sys_info[_ID] = CAMPAIGN[run["dir"] + "/sysinfo/"]

            run_activity = {
                _ID: CAMPAIGN[run["dir"]],
                _TYPE: PROV["Activity"],
                "used": run_used_iris,
                ROBOVAST["success"]: run.get("success"),
                "startedAt": run.get("start_time"),
                "endedAt": run.get("end_time"),
                "wasAssociatedWith": "https://purl.org/robovast/",
                ROBOVAST["sysinfo"]: sys_info[_ID]
            }
            graph.append(run_activity)

            for agent_node in agent_nodes:
                agent_node.setdefault("wasAssociatedWith", []).append(run_activity[_ID])

            rosbag2_prefix = run["dir"] + "/rosbag2/"
            for out_f in run.get("output_files", []):
                if out_f.endswith("csv") or out_f.startswith(rosbag2_prefix):
                    continue
                graph.append({
                    _ID: CAMPAIGN[out_f],
                    _TYPE: PROV["Entity"],
                    "wasGeneratedBy": run_activity[_ID]
                })

            # Rosbag2 entity (combines mcap files and metadata.yaml)
            rosbag2_meta = run.get("rosbag2")
            if rosbag2_meta:
                ros_distro = rosbag2_meta.get("ros_distro", "")
                message_types = rosbag2_meta.get("message_types", [])
                ros_msg_iris = [
                    f"https://docs.ros.org/en/{ros_distro}/p/{msg_type}"
                    for msg_type in message_types
                ] if ros_distro else []

                rosbag2_iri = CAMPAIGN[rosbag2_prefix]
                rosbag2_parts = [
                    CAMPAIGN[rosbag2_prefix + f]
                    for f in rosbag2_meta.get("files", [])
                ]
                rosbag2_parts.append(CAMPAIGN[rosbag2_prefix + "metadata.yaml"])

                rosbag2_node = {
                    _ID: rosbag2_iri,
                    _TYPE: PROV["Entity"],
                    "wasGeneratedBy": run_activity[_ID],
                }
                if ros_distro:
                    rosbag2_node[DCTERMS["hasVersion"]] = ros_distro
                if ros_msg_iris:
                    rosbag2_node[ROBOVAST["ros/messages"]] = ros_msg_iris
                graph.append(rosbag2_node)

                for part_iri in rosbag2_parts:
                    graph.append({
                        _ID: part_iri,
                        _TYPE: PROV["Entity"],
                        "wasGeneratedBy": run_activity[_ID],
                        PROV["hadMember"]: rosbag2_iri,
                    })

            graph.append(sys_info)

    graph.extend(agent_nodes)

    # --- Postprocessing provenance ---
    pp_data = metadata.get("postprocessing", {})
    pp_entries = pp_data.get("entries", [])
    if pp_entries:
        pp_activity = {
            _ID: DATASET[campaign + "postprocessing/"],
            _TYPE: [PROV["Activity"], ROBOVAST["Postprocessing"]],
            "wasAssociatedWith": "https://purl.org/robovast/",
            "wasInfluencedBy": campaign_activity[_ID],
        }
        graph.append(pp_activity)

        for pp_entry in pp_entries:
            output_path = pp_entry.get("output", "")
            # Paths in postprocessing.yaml are relative to _transient/,
            # strip leading "../" to get campaign-relative paths.
            if output_path.startswith("../"):
                output_path = output_path[3:]

            sources = pp_entry.get("sources", [])
            source_iris = []
            for src in sources:
                src_path = src[3:] if src.startswith("../") else src
                source_iris.append(CAMPAIGN[src_path])

            output_node = {
                _ID: CAMPAIGN[output_path],
                _TYPE: PROV["Entity"],
                "wasGeneratedBy": pp_activity[_ID],
            }
            if source_iris:
                output_node["wasDerivedFrom"] = source_iris
            plugin_name = pp_entry.get("plugin")
            if plugin_name:
                output_node[ROBOVAST["plugin"]] = plugin_name
            graph.append(output_node)

    # Compact the JSON-LD graph
    document = {"@graph": graph}
    document.update(iri_context)
    compact = jsonld.compact(document, _BASE_CONTEXT, {"expandContext": _BASE_CONTEXT, "graph": True})
    expanded = jsonld.expand(compact)
    flattened = jsonld.flatten(expanded)
    compact2 = jsonld.compact(flattened, iri_context, {"graph": True})

    prov_json_path = campaign_dir / "metadata.prov.json"
    with open(prov_json_path, "w", encoding="utf-8") as f:
        json.dump(compact2, f, indent=2)

    # Optional: generate PDF visualisation via Graphviz
    dot_path = campaign_dir / "metadata.dot"
    pdf_path = campaign_dir / "metadata.pdf"
    try:
        g = load_graph(str(prov_json_path))
        with open(dot_path, "w+", encoding="utf-8") as dotfile:
            rdf2dot(g, dotfile)
        subprocess.run(
            ["dot", "-Tpdf", str(dot_path), "-o", str(pdf_path)],
            check=False,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not generate provenance PDF (dot not available?): %s", e)

    return True, f"PROV metadata written to {prov_json_path}"


# ---------------------------------------------------------------------------
# Standalone entry point (legacy usage)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import glob
    import yaml

    campaigns = glob.glob("results/*/")

    for campaign in campaigns:
        with open(os.path.join(campaign, "metadata.yaml"), "r") as f:
            metadata = yaml.safe_load(f)
        success, msg = generate_prov_metadata(Path(campaign), metadata)
        print(msg)
