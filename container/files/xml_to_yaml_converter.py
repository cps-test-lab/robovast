#!/usr/bin/env python3
"""
Convert test.xml to test.yaml with custom format
"""

import sys
import os
import xml.etree.ElementTree as ET
import yaml
from pathlib import Path
import html


def convert_xml_to_yaml(xml_file_path, output_dir):
    """Convert test.xml to test.yaml with the specified format"""

    if not os.path.exists(xml_file_path):
        print(f"Warning: {xml_file_path} not found, skipping conversion")
        return

    try:
        # Parse XML
        tree = ET.parse(xml_file_path)
        root = tree.getroot()

        # Extract testsuite data
        testsuite = root if root.tag == "testsuite" else root.find("testsuite")
        if testsuite is None:
            print("Warning: No testsuite element found in XML")
            return

        # Extract testcase data (assuming single testcase)
        testcase = testsuite.find("testcase")
        if testcase is None:
            print("Warning: No testcase element found in XML")
            return

        # Extract failure information
        failure_element = testcase.find("failure")
        failure_data = None
        if failure_element is not None and failure_element.text:
            # Decode HTML entities and preserve formatting
            message_text = html.unescape(failure_element.text.strip())
            failure_data = {"message": message_text}

        # Create YAML structure
        test_data = {
            "errors": int(testsuite.get("errors", 0)),
            "failures": int(testsuite.get("failures", 0)),
            "duration": float(testsuite.get("time", 0)),
        }

        # Add failure if it exists
        if failure_data:
            test_data["testcase"]["failure"] = failure_data

    except Exception as e:
        print(f"Error converting test.xml to YAML: {e}")

    print(f"Successfully converted test.xml to test.yaml")


def get_run_id(run_yaml_path):
    run_id = None
    if os.path.exists(run_yaml_path):
        try:
            with open(run_yaml_path, "r") as f:
                run_data = yaml.safe_load(f)
        except Exception as e:
            print(f"Warning: Could not read run.yaml: {e}")

        if run_data and "RUN_ID" in run_data:
            run_id = run_data["RUN_ID"]
    return run_id


def get_run_prov(output_dir):
    # Get run information from run.yaml
    run_yaml_path = os.path.join(output_dir, "run.yaml")
    run_id = get_run_id(run_yaml_path)

    # Find rosbag directory
    rosbag_dir = os.path.join(output_dir, "rosbag2")
    if os.path.exists(rosbag_dir):
        rosbag_file = f"rosbag2/"

    run_data = {
        "rosbag_file": rosbag_file,
        "run_id": run_id,
    }

    # Write YAML file
    yaml_file_path = os.path.join(output_dir, "test.yaml")
    with open(yaml_file_path, "w") as f:
        yaml.dump(run_data, f, default_flow_style=None)


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 xml_to_yaml_converter.py <output_directory>")
        sys.exit(1)

    output_dir = sys.argv[1]
    xml_file_path = os.path.join(output_dir, "test.xml")

    convert_xml_to_yaml(xml_file_path, output_dir)


if __name__ == "__main__":
    main()
