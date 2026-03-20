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
PDF visualization from the ``metadata.yaml`` written per campaign.  This step
runs automatically after ``metadata.yaml`` is written during
``generate_campaign_metadata``; it does **not** depend on any postprocessing
plugin configuration.

Domain-specific provenance nodes (e.g. map entities for navigation campaigns)
are contributed by variation plugins via the
:meth:`~robovast.common.variation.base_variation.Variation.collect_prov_metadata`
hook.

Requires: ``rdflib`` and ``pyld``.
"""
import datetime as dt
import json
import yaml
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Tuple

import rdflib
from pyld import jsonld
from rdflib import DCTERMS, Namespace, PROV
from rdflib.tools.rdf2dot import rdf2dot

from robovast.common.variation.loader import load_variation_classes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Namespace constants (domain-agnostic)
# ---------------------------------------------------------------------------

_DEFAULT_DATASET_IRI = "https://purl.org/robovast/datasets/default/"

SCENARIOS = Namespace("https://purl.org/secorolab/metamodels/scenarios/osc/")
ROBOVAST = Namespace("https://purl.org/robovast/metamodels/")
MAP_METADATA = Namespace("https://purl.org/secorolab/metamodels/environment#")
QUDT = Namespace("http://qudt.org/schema/qudt/")
QUDT_UNIT = Namespace("http://qudt.org/vocab/unit/")

# JSON-LD context helpers
_ID = "@id"
_CONTEXT = "@context"
_TYPE = "@type"

_BASE_CONTEXT = {
    _CONTEXT: [
        "https://secorolab.github.io/metamodels/prov.json",
        "https://secorolab.github.io/metamodels/metadata.json",
        "https://raw.githubusercontent.com/cps-test-lab/metamodels/refs/heads/main/robovast.json"
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
        _CONTEXT: [
            "https://secorolab.github.io/metamodels/prov.json",
            "https://secorolab.github.io/metamodels/metadata.json",
            "https://raw.githubusercontent.com/cps-test-lab/metamodels/refs/heads/main/robovast.json",
            {
                "agn": "https://purl.org/secorolab/metamodels/agent#",
                "smm": "https://purl.org/secorolab/metamodels/scenarios/osc/",
                "env": "https://purl.org/secorolab/metamodels/environment#",
                "robovast": "https://purl.org/robovast/metamodels/",
                "xsd": "http://www.w3.org/2001/XMLSchema#",
                "prov": "http://www.w3.org/ns/prov#",
                "dcterms": "http://purl.org/dc/terms/",
                "qudt": "http://qudt.org/schema/qudt/",
                "unit": "http://qudt.org/vocab/unit/",
                "dataset": dataset_iri,
            }
        ]
    }


def _build_agents(
    agents_config: List[dict],
    run_files: list,
    campaign_ns: Namespace,
) -> Tuple[list, list, list, list]:
    """Build PROV Agent nodes from the agents configuration.

    Each agent's ``configuration_files`` (paths relative to the configuration
    root, e.g. ``files/nav2_launch.py``) are matched against *run_files*
    (which carry a ``_config/`` prefix, e.g. ``_config/files/nav2_launch.py``)
    to build per-agent ``prov:hadPlan`` relations.

    A single *aggregate plan entity* is created per agent under
    ``plans/{agent_id}-plan``.  The agent's ``prov:hadPlan`` points to that
    single plan, and the plan node aggregates the individual configuration
    files via ``prov:hadMember`` — following the clean PROV-O pattern where
    ``prov:hadPlan`` targets exactly one plan entity.

    Returns:
        Tuple of (agent_nodes, extra_nodes) where *extra_nodes* contains both
        the individual config-file entities and the per-agent plan nodes.
    """
    # Build a lookup: path without "_config/" prefix → full run_file string
    run_files_lookup: dict = {}
    for rf in run_files:
        stripped = rf[len("_config/"):] if rf.startswith("_config/") else rf
        run_files_lookup[stripped] = rf

    # Individual config-file entities (typed as Entity; aggregated by plan nodes)
    location_nodes = [
        {_ID: campaign_ns[rf], _TYPE: PROV["Entity"]}
        for rf in run_files
    ]

    agent_nodes: list = []
    agent_loading: list = []
    plan_nodes: list = []
    for agent_cfg in agents_config:
        agent_cfg = dict(agent_cfg)
        # .vast uses "id"; fall back to legacy "name" key
        agent_id = agent_cfg.pop("id", agent_cfg.pop("name", "agent"))
        config_files = agent_cfg.pop("configuration_files", [])

        # Match each configuration_file to its plan IRI
        agent_plan_iris = []
        for cf in config_files:
            matched_rf = run_files_lookup.get(cf)
            if matched_rf is not None:
                agent_plan_iris.append(campaign_ns[matched_rf])
            else:
                logger.warning(
                    "Agent '%s': configuration_file '%s' not found in run_files",
                    agent_id, cf,
                )

        agent_node: dict = {
            _ID: campaign_ns[agent_id],
            _TYPE: PROV["SoftwareAgent"],
        }
        agent_load: dict = {
            _ID: campaign_ns[agent_id+"/load/"],
            _TYPE: PROV["Activity"],
            "wasAssociatedWith": agent_node[_ID],
        }
        if agent_plan_iris:
            # One aggregate plan entity per agent: agent → plan → files
            plan_iri = campaign_ns[f"config/{agent_id}-config"]
            plan_node = {
                _ID: plan_iri,
                _TYPE: [PROV["Entity"], PROV["Collection"]],
                PROV["hadMember"]: agent_plan_iris,
            }
            plan_nodes.append(plan_node)
            agent_load[PROV["used"]] = plan_iri
            agent_loading.append(agent_load)

        # Remaining keys become properties on the agent node
        for k, v in agent_cfg.items():
            agent_node[ROBOVAST[k]] = v
        agent_nodes.append(agent_node)

    return agent_nodes, location_nodes, plan_nodes, agent_loading

def _build_vast_config(vast_path, config_dir, campaign_ns, abstract_scenario_id, metadata):
    with open(os.path.join(config_dir, vast_path), "r") as f:
        vast_config = yaml.safe_load(f)

    configs = []
    variations = []
    for config in vast_config.get("configuration", []):
        logical_scen = {
            _ID: campaign_ns[config.get("name")],
            _TYPE: [PROV["Entity"], SCENARIOS["LogicalScenario"]],
        }
        for variation in config.get("variations", []):
            var_type, params = variation.popitem()
            var_config = {
                _ID: campaign_ns[config.get("name")+"/variations/"+var_type+"Config"],
                _TYPE: [PROV["Entity"], ROBOVAST[f"variations/{var_type}Config"]],
            }
            if var_type in ["PathVariationRandom", "ObstacleVariation", "ObstacleVariationWithDistanceTrigger", "PathVariationRasterized"]:
                var_config[QUDT["hasUnit"]] = QUDT_UNIT["M"]
            for k, v in params.items():
                if k == "map_file" or k == "mesh_file":
                    var_config[k] = campaign_ns[v]
                elif k == "floorplans":
                    var_config[k] = [campaign_ns[m] for m in v]
                elif k == "obstacle_configs":
                    for p in v:
                        file_path = p["model"][8:]
                        p["model"] = campaign_ns[f"_{file_path}"]
                    var_config[k] = v
                elif k == "name":
                    param_name = var_config.setdefault("param_name", [])
                    if isinstance(v, list):
                        param_name.extend(v)
                        if var_type == "ParameterVariationList":
                            param_value = var_config.setdefault("param_values", [])
                            for val in params["values"]:
                                new_vals = []
                                for n, vv in zip(v, val):
                                    if n == "map_file" or n == "mesh_file":
                                        new_vals.append(campaign_ns[vv])
                                    else:
                                        new_vals.append(vv)
                                param_value.append(new_vals)
                    else:
                        param_name.append(v)
                        if var_type == "ParameterVariationList":
                            param_value = var_config.setdefault("param_values", [])
                            if v == "map_file" or v == "mesh_file":
                                for val in params["values"]:
                                    param_value.append(campaign_ns[val])
                            else:
                                param_value.extend(params["values"])
                elif k == "values":
                    continue
                elif k == "goal_pose" or k == "goal_poses" or k == "start_pose":
                    if isinstance(v, str):
                        param_name = var_config.setdefault("param_name", [])
                        param_name.append(v)
                    else:
                        var_config[k] = v
                elif k == "variations":
                    # TODO Need a better way to handle potentially nested OneOfVariation
                    var_within_var = []
                    for vv_conf in v:
                        vv_type, vv_params = vv_conf.popitem()
                        _vvar_config = {
                            _ID: campaign_ns[
                                config.get("name")
                                + "/variations/"
                                + vv_type
                                + "Config"
                            ],
                            _TYPE: [
                                PROV["Entity"],
                                ROBOVAST[f"variations/{vv_type}Config"],
                            ],
                            **vv_params,
                        }
                        var_within_var.append(_vvar_config)
                    var_config[k] = var_within_var
                else:
                    var_config[k] = v

            logical_scen.setdefault("variations", []).append(var_config[_ID])
            variations.append(var_config)
            configs.append(logical_scen)

    return configs, variations


def generate_prov_metadata(
    campaign_dir: Path,
    metadata: dict,
    generate_visualization: bool = True,
) -> Tuple[bool, str]:
    """Generate a PROV-O provenance graph from campaign metadata.

    Reads the already-computed *metadata* dict (the same data that was just
    written to ``metadata.yaml``) and produces:

    * ``<campaign_dir>/metadata.prov.json``  -- compact JSON-LD provenance graph
    * ``<campaign_dir>/metadata.dot``        -- Graphviz DOT file (if *generate_visualization*)
    * ``<campaign_dir>/metadata.pdf``        -- visualization (if *generate_visualization*,
                                               requires Graphviz ``dot``)

    Campaign-level configuration is read from ``metadata["metadata"]``:

    * ``dataset_iri`` -- base IRI for the dataset namespace
    * ``agents``      -- list of ``{name, type, ...}`` dicts for PROV Agent nodes
                         (optional; omit for campaigns with no robot/agent)

    Domain-specific provenance nodes are contributed by variation plugins
    via :meth:`Variation.collect_prov_metadata`.

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.
        metadata:     The metadata dict that was written to ``metadata.yaml``.
        generate_visualization: When ``True`` (default), also write
            ``metadata.dot`` and render ``metadata.pdf`` via Graphviz.
            Set to ``False`` to skip DOT/PDF generation.

    Returns:
        Tuple of ``(success, message)``.
    """
    start_t = dt.datetime.now().isoformat()
    campaign_dir = Path(campaign_dir)
    campaign = campaign_dir.name + "/"

    # --- Read campaign-level configuration from metadata ---
    md_section = dict(metadata.get("metadata", {}))
    dataset_iri = md_section.pop("dataset_iri", _DEFAULT_DATASET_IRI)
    if not dataset_iri.endswith("/"):
        dataset_iri += "/"
    agents_config = md_section.pop("agents", [])

    dataset_ns = Namespace(dataset_iri)
    campaign_ns = Namespace(f"{dataset_iri}{campaign}")
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
        _ID: dataset_ns[campaign + "execution/"],
        _TYPE: [PROV["Activity"], ROBOVAST["CampaignExecution"], ROBOVAST[metadata["execution"]["execution_type"].capitalize()]],
        "startedAtTime": metadata["execution"]["execution_time"],
        "wasAssociatedWith": "https://purl.org/robovast/",
        ROBOVAST["runs"]: metadata["execution"]["runs"]
    }
    graph.append(campaign_activity)

    campaign_entity = {
        _ID: dataset_ns[campaign],
        _TYPE: [PROV["Entity"], ROBOVAST["Campaign"]],
        "wasGeneratedBy": campaign_activity[_ID]
    }
    graph.append(campaign_entity)

    # --- Agent nodes (robots, manipulators, etc.) ---
    agent_nodes, location_nodes, config_collections, agent_loads = _build_agents(
        agents_config, metadata.get("run_files", []), campaign_ns,
    )
    graph.extend(config_collections)
    graph.extend(location_nodes)
    graph.extend(agent_loads)

    # --- Scenario and config generation ---
    scenario_file = metadata.get("scenario_file", "scenario.osc")
    abstract_scenario = {
        _ID: campaign_ns[f"_config/{scenario_file}"],
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

    logical_scenarios, vast_variations = _build_vast_config(vast_file_name, config_dir, campaign_ns, abstract_scenario[_ID], metadata)
    graph.extend(logical_scenarios)
    graph.extend(vast_variations)
    vast_config = {
        _ID: campaign_ns[f"_config/{vast_file_name or 'config.vast'}"],
        _TYPE: [PROV["Entity"], PROV["Collection"], ROBOVAST["VastConfiguration"]],
        "references": abstract_scenario[_ID],
        PROV["hadMember"]: [s[_ID] for s in logical_scenarios]
    }
    graph.append(vast_config)

    gen_activity = {
        _ID: dataset_ns[campaign + "config_generation"],
        _TYPE: [PROV["Activity"], ROBOVAST["ConfigGeneration"]],
        "used": [vast_config[_ID], abstract_scenario[_ID]],
        "wasInfluencedBy": campaign_activity[_ID],
        "wasAssociatedWith": "https://purl.org/robovast/",
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
        config_ns = Namespace(f"{dataset_iri}{campaign}{config_path}")

        config_md = all_configs.get(config_name)
        if config_md is None:
            continue

        # Concrete scenario node
        scenario_node = {
            _ID: campaign_ns[config_path],
            _TYPE: [PROV["Entity"], SCENARIOS["ConcreteScenario"]],
            "wasGeneratedBy": gen_activity[_ID],
            PROV["specializationOf"]: {_ID: abstract_scenario[_ID]},
            PROV["atLocation"]: campaign_ns[config_path+"_config/scenario.config"],
            PROV["generatedAtTime"]: config_md.get("created_at"),
            PROV["wasDerivedFrom"]: config_md.get("derived_from"),
        }
        graph.append({
            _ID: campaign_ns[config_path+"_config/scenario.config"],
            _TYPE: [PROV["Location"]]
        })

        # Collect domain-specific PROV contributions from variation plugins
        run_used_iris = [scenario_node[_ID]]
        for agn_config in config_collections:
            run_used_iris.append(agn_config[_ID])

        for vdata in config_md.get("variations", []):
            vtype_name = vdata.get("name", "")
            cls = variation_classes.get(vtype_name)
            if cls is None or not hasattr(cls, "collect_prov_metadata"):
                continue

            try:
                contribution = cls.collect_prov_metadata(
                    config_entry=config_md,
                    campaign_namespace=campaign_ns,
                    config_namespace=config_ns,
                    gen_activity_id=gen_activity[_ID],
                    vast_id=vast_config[_ID]
                )
            except Exception as e:
                logger.warning(
                    "Variation '%s' collect_prov_metadata failed for '%s': %s",
                    vtype_name, config_name, e,
                )
                continue

            end_t = dt.datetime.fromisoformat(vdata.get("started_at")) + dt.timedelta(seconds=vdata.get("duration"))
            var_node = {
                _ID: campaign_ns[config_path + f"variations/{vtype_name}"],
                _TYPE: [
                    PROV["Activity"],
                    ROBOVAST["Variation"],
                    ROBOVAST[vtype_name]
                ],
                "startedAtTime": vdata.get("started_at"),
                "endedAtTime": end_t.isoformat(),
            }
            graph.append(var_node)

            if contribution is None:
                continue

            scenario_node.update(contribution.scenario_properties)
            scenario_node.setdefault("variations", []).append(var_node[_ID])
            graph.extend(contribution.graph_nodes)
            run_used_iris.extend(contribution.run_used_iris)

        for pdata in config_md.get("parameters", []):
            name = pdata.get("name", "")
            if name == "map_file":
                # Map file entity
                map_file = name
                pgm_iri = campaign_ns[map_file.replace("yaml", "pgm")]
                graph.append(
                    {
                        _ID: pgm_iri,
                        _TYPE: [PROV["Entity"], MAP_METADATA["OccupancyGrid"]],
                    }
                )
                map_iri = campaign_ns[map_file]
                map_file_md = config_md.get("map_file", {})
                graph.append(
                    {
                        _ID: map_iri,
                        _TYPE: [PROV["Entity"], MAP_METADATA["Metadata"]],
                        MAP_METADATA["resolution"]: map_file_md.get("resolution"),
                        "references": pgm_iri,
                        "generatedAt": map_file_md.get("updated_at"),
                    }
                )
                run_used_iris.append(map_iri)
                scenario_node.setdefault("references", []).append(map_iri)
            elif name == "mesh_file":
                # Mesh file entity
                mesh_file = name
                mesh_file_md = config_md.get("mesh_file", {})
                mesh_iri = campaign_ns[mesh_file]
                graph.append(
                    {
                        _ID: mesh_iri,
                        _TYPE: [PROV["Entity"], MAP_METADATA["Mesh3D"]],
                        "generatedAt": mesh_file_md.get("created_at"),
                    }
                )
                run_used_iris.append(mesh_iri)
                scenario_node.setdefault("references", []).append(mesh_iri)

        graph.append(scenario_node)

        # Per-run activities
        for run in config_md.get("test_results", []):
            sysinfo = dict(run.get("sysinfo", {}))
            platform = sysinfo.pop("platform", {})
            sys_info = {**platform, **sysinfo}
            sys_info = {ROBOVAST[k]: v for k, v in sys_info.items()}
            sys_info[_ID] = campaign_ns[run["dir"] + "/sysinfo/"]

            run_activity = {
                _ID: campaign_ns[run["dir"]],
                _TYPE: [PROV["Activity"], ROBOVAST["TestExecution"]],
                "used": run_used_iris,
                ROBOVAST["success"]: run.get("success"),
                "startedAtTime": run.get("start_time"),
                "endedAtTime": run.get("end_time"),
                "wasAssociatedWith": ["https://purl.org/robovast/"],
                ROBOVAST["sysinfo"]: {_ID: sys_info[_ID]}
            }
            for agent_node in agent_nodes:
                run_activity["wasAssociatedWith"].append(agent_node[_ID])
            graph.append(run_activity)

            # Promote sysinfo/ to a first-class prov:Entity with a standard
            # prov:wasGeneratedBy edge pointing to the run activity.  The
            # ROBOVAST["sysinfo"] property on the run activity is kept for
            # backwards compatibility but on its own it is only an indirect,
            # domain-specific link that is harder to traverse semantically.
            sys_info[_TYPE] = PROV["Entity"]
            sys_info["wasGeneratedBy"] = run_activity[_ID]

            rosbag2_prefix = run["dir"] + "/rosbag2/"
            for out_f in run.get("output_files", []):
                if out_f.endswith("csv") or out_f.startswith(rosbag2_prefix):
                    continue
                art_type = [PROV["Entity"]]
                if out_f.endswith("log"):
                    art_type.append(ROBOVAST["LogFile"])
                elif out_f.endswith("xml"):
                    art_type.append(ROBOVAST["TestResult"])
                graph.append(
                    {
                        _ID: campaign_ns[out_f],
                        _TYPE: art_type,
                        "wasGeneratedBy": run_activity[_ID],
                    }
                )

            # Rosbag2 entity (combines mcap files and metadata.yaml)
            rosbag2_meta = run.get("rosbag2")
            if rosbag2_meta:
                ros_distro = rosbag2_meta.get("ros_distro", "")
                message_types = rosbag2_meta.get("message_types", [])
                ros_msg_iris = [
                    f"https://docs.ros.org/en/{ros_distro}/p/{msg_type}"
                    for msg_type in message_types
                ] if ros_distro else []

                rosbag2_iri = campaign_ns[rosbag2_prefix]
                rosbag2_parts = [
                    campaign_ns[rosbag2_prefix + f]
                    for f in rosbag2_meta.get("files", [])
                ]
                rosbag2_parts.append(campaign_ns[rosbag2_prefix + "metadata.yaml"])

                rosbag2_node = {
                    _ID: rosbag2_iri,
                    _TYPE: [PROV["Collection"], ROBOVAST["ROSBag"]],
                    "wasGeneratedBy": run_activity[_ID],
                    PROV["hadMember"]: rosbag2_parts,
                }
                if ros_distro:
                    rosbag2_node[DCTERMS["hasVersion"]] = ros_distro
                if ros_msg_iris:
                    rosbag2_node[ROBOVAST["ros/messages"]] = ros_msg_iris
                graph.append(rosbag2_node)

                for part_iri in rosbag2_parts:
                    part_type = [PROV["Entity"]]
                    if part_iri.endswith(".mcap"):
                        part_type.append(ROBOVAST["MCAPFile"])
                    elif part_iri.endswith(".yaml"):
                        part_type.append(ROBOVAST["BagFile#Metadata"])
                    graph.append({
                        _ID: part_iri,
                        _TYPE: part_type,
                        "wasGeneratedBy": run_activity[_ID],
                    })

            graph.append(sys_info)

    graph.extend(agent_nodes)

    # --- Postprocessing provenance ---
    pp_data = metadata.get("postprocessing", {})
    pp_entries = pp_data.get("entries", [])
    if pp_entries:
        pp_activity = {
            _ID: dataset_ns[campaign + "postprocessing/"],
            _TYPE: [PROV["Activity"], ROBOVAST["Postprocessing"]],
            "wasAssociatedWith": "https://purl.org/robovast/",
            "wasInfluencedBy": campaign_activity[_ID],
        }

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
                if src_path.endswith("rosbag2"):
                    # IRIS must end in / to match the rosbag2 collection above
                    src_path = src_path + "/"
                source_iris.append(campaign_ns[src_path])

            output_node = {
                _ID: campaign_ns[output_path],
                _TYPE: [PROV["Entity"], ROBOVAST["Traces"]],
                "wasGeneratedBy": pp_activity[_ID],
            }
            if source_iris:
                output_node["wasDerivedFrom"] = source_iris
                pp_activity.setdefault("used", []).extend(source_iris)
            plugin_name = pp_entry.get("plugin")
            if plugin_name:
                output_node[ROBOVAST["plugin"]] = plugin_name
            graph.append(output_node)

        graph.append(pp_activity)

        # --- Postprocessing metadata and provenance activities ---
        metadata_activity = {
            _ID: campaign_ns["postprocessing/metadata/"],
            _TYPE: [PROV["Activity"], ROBOVAST["PostprocessingMetadata"]],
            "wasAssociatedWith": "https://purl.org/robovast/",
            "wasInfluencedBy": pp_activity[_ID],
            "used": campaign_entity[_ID],
        }
        metadata_node = {
            _ID: campaign_ns["metadata.yaml"],
            _TYPE: [PROV["Entity"], ROBOVAST["Campaign#Metadata"]],
            "wasGeneratedBy": metadata_activity[_ID],
        }
        graph_activity = {
            _ID: campaign_ns["postprocessing/graph/"],
            _TYPE: [PROV["Activity"], ROBOVAST["PostprocessingGraph"]],
            "wasAssociatedWith": "https://purl.org/robovast/",
            "wasInfluencedBy": metadata_activity[_ID],
            "used": metadata_node[_ID],
            "startedAtTime": start_t,
            "endedAtTime": dt.datetime.now().isoformat(),
        }
        graph.append({
            _ID: campaign_ns["metadata.prov.json"],
            _TYPE: [PROV["Entity"], ROBOVAST["Campaign#Graph"]],
            "wasGeneratedBy": graph_activity[_ID],
            "wasDerivedFrom": metadata_node[_ID],
        })

        graph.append(metadata_node)
        graph.append(metadata_activity)
        graph.append(graph_activity)

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

    # Optional: generate DOT/PDF visualization via Graphviz
    dot_path = campaign_dir / "metadata.dot"
    pdf_path = campaign_dir / "metadata.pdf"
    if generate_visualization:
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
