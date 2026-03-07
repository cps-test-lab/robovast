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

Requires: ``rdflib`` and ``pyld``.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

import rdflib
from rdflib import Namespace, PROV, DCTERMS
from rdflib.tools.rdf2dot import rdf2dot
from pyld import jsonld

# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------

DATASET_IRI = "https://purl.org/robovast/datasets/2026-03-07-robovast-navigation/"

SCENARIOS = Namespace("https://secorolab.github.io/metamodels/scenarios/osc/")
ROBOVAST = Namespace("https://purl.org/robovast/metamodels/")
MAP_METADATA = Namespace("https://purl.org/secorolab/metamodels/environment#")
DATASET = Namespace(DATASET_IRI)

# JSON-LD context helpers
_ID = "@id"
_CONTEXT = "@context"
_TYPE = "@type"

_IRI_CONTEXT = {
    _CONTEXT: {
        "agn": "https://secorolab.github.io/metamodels/agent#",
        "smm": "https://secorolab.github.io/metamodels/scenarios#",
        "env": "https://secorolab.github.io/metamodels/environment#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "prov": "http://www.w3.org/ns/prov#",
        "dct": "http://purl.org/dc/terms/",
        "dataset": DATASET_IRI,
    }
}

_BASE_CONTEXT = {
    _CONTEXT: [
        "https://secorolab.github.io/metamodels/prov.json"
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
    g.bind("env", MAP_METADATA)
    g.bind("dataset", DATASET)
    return g


def generate_prov_metadata(
    campaign_dir: Path,
    metadata: dict,
) -> Tuple[bool, str]:
    """Generate a PROV-O provenance graph from campaign metadata.

    Reads the already-computed *metadata* dict (the same data that was just
    written to ``metadata.yaml``) and produces:

    * ``<campaign_dir>/metadata.prov.json``  — compact JSON-LD provenance graph
    * ``<campaign_dir>/metadata.pdf``        — visualisation (requires Graphviz ``dot``)

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.
        metadata:     The metadata dict that was written to ``metadata.yaml``.

    Returns:
        Tuple of ``(success, message)``.
    """
    import json  # noqa: PLC0415

    campaign_dir = Path(campaign_dir)
    # The relative path segment used as key in DATASET namespace (e.g. "results/campaign-0/")
    campaign = str(campaign_dir) + "/"

    CAMPAIGN = Namespace(f"{DATASET_IRI}{campaign}")

    graph = []

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

    robot_files = [CAMPAIGN[l] for l in metadata["run_files"]]

    agent = dict(metadata.get("metadata", {}))
    agent[_ID] = CAMPAIGN["turtlebot4"]  # TODO: make configurable
    agent[_TYPE] = PROV["Agent"]
    agent["atLocation"] = robot_files

    for robot_file in robot_files:
        graph.append({
            _ID: robot_file,
            _TYPE: PROV["Location"],
        })

    abstract_scenario = {
        _ID: CAMPAIGN["_config/scenario.osc"],
        _TYPE: [PROV["Entity"], SCENARIOS["AbstractScenario"]],
    }
    graph.append(abstract_scenario)

    vast_config = {
        _ID: CAMPAIGN["_config/most_important.vast"],
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

    all_configs = {c["name"]: c for c in metadata["configurations"]}

    config_dir_path = campaign_dir
    configs = sorted(
        str(p) + "/"
        for p in config_dir_path.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )

    for config_path in configs[:1]:  # TODO: remove [:1] when ready for all configs
        CONFIG = Namespace(f"{DATASET_IRI}{config_path}")
        config = os.path.split(os.path.split(config_path)[0])[-1]

        config_md = all_configs.get(config)
        if config_md is None:
            continue

        n = {
            _ID: CAMPAIGN[config_path],
            _TYPE: [PROV["Entity"], SCENARIOS["ConcreteScenario"]],
            "wasGeneratedBy": gen_activity[_ID],
        }
        variations = config_md.get("variations", [])
        if variations:
            n["references"] = CAMPAIGN[variations[0].get("fpm_file", "")]

        if isinstance(config_md.get("config", {}).get("goal_pose"), dict):
            n[ROBOVAST["goals"]] = 1
        else:
            goal_pose = config_md.get("config", {}).get("goal_pose", [])
            n[ROBOVAST["goals"]] = len(goal_pose) if goal_pose else 0
        n[ROBOVAST["obstacles"]] = len(
            config_md.get("config", {}).get("static_objects", [])
        )
        graph.append(n)

        fpm_activity = {
            _ID: CAMPAIGN[config_path + "jsonld_generation/"],
            _TYPE: PROV["Activity"],
            "used": CAMPAIGN[variations[0].get("fpm_file", "")] if variations else None,
            "wasAssociatedWith": "https://purl.org/secorolab/scenery_builder",
            "wasInfluencedBy": gen_activity[_ID]
        }
        graph.append(fpm_activity)

        map_file_md = config_md.get("map_file", {})
        json_files = [
            CAMPAIGN[l]
            for l in map_file_md.get("derived_from", [])
            if l.endswith(".json")
        ]

        jsonld_activity = {
            _ID: CAMPAIGN[config + "artefact_generation/"],
            _TYPE: PROV["Activity"],
            "used": json_files,
            "wasAssociatedWith": "https://purl.org/secorolab/scenery_builder",
            "wasInfluencedBy": [gen_activity[_ID], fpm_activity[_ID]]
        }

        for j in json_files:
            graph.append({
                _ID: j,
                _TYPE: PROV["Entity"],
                "wasGeneratedBy": fpm_activity[_ID]
            })

        config_cfg = config_md.get("config", {})
        graph.append({
            _ID: CONFIG[config_cfg.get("map_file", "")],
            _TYPE: PROV["Entity"],
            "wasGeneratedBy": jsonld_activity[_ID],
            MAP_METADATA["resolution"]: map_file_md.get("resolution"),
            "references": CAMPAIGN[
                config_cfg.get("map_file", "").replace("yaml", "pgm")
            ],
            "generatedAt": map_file_md.get("updated_at")
        })
        mesh_file_md = config_md.get("mesh_file", {})
        graph.append({
            _ID: CONFIG[config_cfg.get("mesh_file", "")],
            _TYPE: PROV["Entity"],
            "wasGeneratedBy": jsonld_activity[_ID],
            "generatedAt": mesh_file_md.get("created_at")
        })

        for run in config_md.get("test_results", []):
            sysinfo = dict(run.get("sysinfo", {}))
            platform = sysinfo.pop("platform", {})
            sys_info = dict(**platform, **sysinfo)
            sys_info = {ROBOVAST[k]: v for k, v in sys_info.items()}
            sys_info[_ID] = CAMPAIGN[run["dir"] + "/sysinfo/"]

            run_activity = {
                _ID: CAMPAIGN[run["dir"]],
                _TYPE: PROV["Activity"],
                "used": [
                    n[_ID],
                    CONFIG[config_cfg.get("map_file", "")],
                    CONFIG[config_cfg.get("mesh_file", "")]
                ],
                ROBOVAST["success"]: run.get("success"),
                "startedAt": run.get("start_time"),
                "endedAt": run.get("end_time"),
                "wasAssociatedWith": "https://purl.org/robovast/",
                ROBOVAST["sysinfo"]: sys_info[_ID]
            }
            graph.append(run_activity)
            agent.setdefault("wasAssociatedWith", []).append(run_activity[_ID])

            for out_f in run.get("output_files", []):
                if out_f.endswith("csv"):
                    continue
                graph.append({
                    _ID: CAMPAIGN[out_f],
                    _TYPE: PROV["Entity"],
                    "wasGeneratedBy": run_activity[_ID]
                })

            graph.append(sys_info)

    graph.append(agent)

    # Compact the JSON-LD graph
    document = {"@graph": graph}
    document.update(_IRI_CONTEXT)
    compact = jsonld.compact(document, _BASE_CONTEXT, {"expandContext": _BASE_CONTEXT, "graph": True})
    expanded = jsonld.expand(compact)
    flattened = jsonld.flatten(expanded)
    compact2 = jsonld.compact(flattened, _IRI_CONTEXT, {"graph": True})

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