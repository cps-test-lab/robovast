#!/usr/bin/env python3
"""
Convert test.xml to test.yaml with custom format
"""

import sys
import os
import glob
import json

import yaml


def get_run_data(run_yaml_path):
    if os.path.exists(run_yaml_path):
        try:
            with open(run_yaml_path, "r") as f:
                run_data = yaml.safe_load(f)
        except Exception as e:
            print(f"Warning: Could not read run.yaml: {e}")

    return run_data


def _create_abstract_scenario(scenario_id):
    return {
        "@id": f"scenarios:{scenario_id}",
        "@type": ["AbstractScenario", "Entity"],
        "atLocation": f"scenarios:{scenario_id}",
    }


def _create_concrete_scenario(scenario_id, location=None, parent_scenario_id=None, **kwargs):
    node = {
        "@id": f"scenarios:{scenario_id}",
        "@type": ["ConcreteScenario", "Entity"],
    }
    if kwargs.get("gen_time") is not None:
        node["generatedAtTime"] = kwargs["gen_time"]
    if kwargs.get("source_files") is not None:
        node["wasGeneratedBy"] = {
            "uses": kwargs.get("source_files"),
        }
    if location is not None:
        node["atLocation"] = f"scenarios:{scenario_id}"
    if parent_scenario_id is not None:
        node["specializationOf"] = f"scenarios:{parent_scenario_id}"
    return node


def _create_run_activity(run_mdata):
    return {
        "@id": f"run:{run_mdata['RUN_ID']}",
        "@type": ["Activity", "TestRun"],
        "startedAtTime": run_mdata["START_DATE"],
        "endedAtTime": run_mdata["END_DATE"],
        "used": f"scenario:{run_mdata['SCENARIO_ID']}",
        "wasAssociatedWith": f"agents:{run_mdata['ROBOT_ID']}",
    }


def _create_generated_artefact(artefact_id, activity=None, source_artefact_id=None):
    node = {
        "@id": f"run:{artefact_id}",
        "@type": ["Entity", "Artefact"],
        "atLocation": f"{artefact_id}",
    }
    if activity is not None:
        node["wasGeneratedBy"] = f"run:{activity}"
    if source_artefact_id is not None:
        node["wasDerivedFrom"] = f"run:{source_artefact_id}"

    return node


def _gen_jsonld_prov(out_dir, run_data):
    graph = []

    _scenario_id = run_data["SCENARIO_ID"]
    scenario = _create_concrete_scenario(_scenario_id)
    graph.append(scenario)

    _run_id = run_data["RUN_ID"]
    run = _create_run_activity(run_data)
    graph.append(run)

    artefact_paths = [
        f"{run_data['ROSBAG_DIR']}/**",
        f"{run_data['LOG_DIR']}/**/*.log",
        "*.webm",
        "*.xml",
    ]
    artefacts = []
    for artefact_file in artefact_paths:
        files = glob.glob(os.path.join(out_dir, artefact_file), recursive=True)
        artefacts.extend(files)

    for rosbag_file in artefacts:
        bag_path = os.path.relpath(rosbag_file, out_dir)
        art_id = f"{_run_id}/{bag_path}"
        bag = _create_generated_artefact(art_id, activity=_run_id)
        graph.append(bag)

    return graph


def get_run_prov(run_output_dir):
    # Get run information from run.yaml
    run_yaml_path = os.path.join(run_output_dir, "run.yaml")
    run_data = get_run_data(run_yaml_path)

    # Find rosbag directory
    rosbag_dir = os.path.join(run_output_dir, "rosbag2")
    if os.path.exists(rosbag_dir):
        rosbag_file = f"rosbag2/"

    run_data["ROSBAG_DIR"] = rosbag_file
    prov_data = _gen_jsonld_prov(run_output_dir, run_data)

    # Write JSON file
    output_path = os.path.join(run_output_dir, "run.prov.json")
    with open(output_path, "w") as f:
        json.dump(prov_data, f, indent=4)


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 xml_to_yaml_converter.py <output_directory>")
        sys.exit(1)

    output_dir = sys.argv[1]
    xml_file_path = os.path.join(output_dir, "test.xml")

    # convert_xml_to_yaml(xml_file_path, output_dir)
    get_run_prov(output_dir)


if __name__ == "__main__":
    main()
