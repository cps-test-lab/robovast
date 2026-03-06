#!/usr/bin/env python3
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

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional, Union

import pandas as pd
import yaml


def get_scenario_parameter(data_dir: str, parameter_name: str):
    """
    Get a specific scenario parameter from the scenario configuration file.

    Args:
        data_dir (str): Path to the directory containing the scenario.config file.
        parameter_name (str): Name of the parameter to retrieve.
    Returns:
        The value of the specified parameter, or None if not found.
    """
    config_path = Path(data_dir) / "_config" / "scenario.config"
    if not config_path.exists():
        raise FileNotFoundError(f"scenario.config not found in {data_dir}")

    with open(config_path, 'r') as f:
        config_content = yaml.safe_load(f)

        # skip scenario-name
        if isinstance(config_content, dict) and len(config_content) == 1:
            config_content = next(iter(config_content.values()))

        return config_content.get(parameter_name, None)


def read_output_files(data_dir: str, reader_func: Callable[[Path], pd.DataFrame], debug: bool = False) -> pd.DataFrame:
    """
    Reads and combines output data from all run subdirectories within data_dir.

    Args:
        data_dir (str): Path to the directory containing run subdirectories.
        reader_func (Callable[[Path], pd.DataFrame]): Function that reads run data from a directory and returns a DataFrame.
        debug (bool, optional): If True, prints debug information. Defaults to False.

    Returns:
        pd.DataFrame: Combined DataFrame containing all run data, with additional columns for run, config, and scenario parameters.

    Raises:
        ValueError: If data_dir does not exist, no test.xml files are found, or no valid run data could be read.
    """
    data_path = Path(data_dir)

    if not data_path.exists():
        raise ValueError(f"Data directory does not exist: {data_dir}")

    all_dataframes = []
    skipped_warnings = []

    # Find all test.xml files in subdirectories
    run_xml_files = list(data_path.rglob("test.xml"))

    if not run_xml_files:
        raise ValueError(f"No test.xml files found in subdirectories of {data_dir}")

    if debug:
        print(f"Found {len(run_xml_files)} run directories")

    category_names = set({'run', 'config'})
    for run_xml in run_xml_files:
        if debug:
            print(f"Reading data from: {run_xml}")
        run_dir = run_xml.parent
        run_name = run_dir.name

        try:
            # Call the user-provided reader function
            if reader_func:
                df = reader_func(run_dir)
            else:
                df = pd.DataFrame()
            scenario_config_path = run_dir.parent / "_config" / "scenario.config"
            config_parameters = {}
            try:
                with open(scenario_config_path, 'r') as f:
                    config_content = yaml.safe_load(f)

                    # skip scenario-name
                    if isinstance(config_content, dict) and len(config_content) == 1:
                        config_parameters = next(iter(config_content.values()))
            except Exception as e:
                if debug:
                    print(f"Could not read scenario.config: {e}\n")

            df['run'] = str(run_name)
            df['config'] = str(os.path.basename(run_dir.parent))
            category_names.update(config_parameters.keys())
            for param_name, param_value in config_parameters.items():
                if isinstance(param_value, (dict, list)):
                    df[param_name] = yaml.safe_dump(param_value)
                else:
                    df[param_name] = param_value

            all_dataframes.append(df)

        except Exception as e:
            skipped_warnings.append(f"{run_dir}: {e}")
            continue

    if skipped_warnings and debug:
        print(f"Warning: Could not read data from {len(skipped_warnings)} run(s): " +
              "; ".join(skipped_warnings))

    if debug and not all_dataframes:
        raise ValueError(f"No valid run data could be read from {data_dir}")

    # Combine all dataframes
    combined_df = pd.concat(all_dataframes, ignore_index=True)

    for category in category_names:
        combined_df[category] = combined_df[category].astype('category')

    if debug:
        print(f"Combined dataframe shape: {combined_df.shape}")
        print(f"Columns: {list(combined_df.columns)}")
        print(f"Number of unique runs: {combined_df['run'].nunique()}")

    return combined_df


def read_output_csv(run_dir: str, filename: str, skiprows: int = 0) -> pd.DataFrame:
    """
    Read a CSV file from a run directory, skipping the first line (comment).

    Args:
        run_dir: Path to the run directory as a string
        filename: Name of the CSV file to read

    Returns:
        DataFrame with the CSV data
    """
    csv_path = os.path.join(run_dir, filename)
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"{filename} not found in {run_dir}")

    # Read CSV, skipping the first line (comment)
    df = pd.read_csv(Path(csv_path), skiprows=skiprows)
    return df


def _flatten_value(value, key: str, level: int, merge_level: int) -> dict:
    """Flatten a value (dict, list, or scalar) into top-level key-value pairs."""
    if isinstance(value, dict) and level < merge_level:
        result = {}
        for k, v in value.items():
            subkey = f"{key}_{k}" if key else k
            result.update(_flatten_value(v, subkey, level + 1, merge_level))
        return result
    elif isinstance(value, list) and level < merge_level:
        result = {}
        for i, elem in enumerate(value):
            subkey = f"{key}_{i}" if key else str(i)
            result.update(_flatten_value(elem, subkey, level + 1, merge_level))
        return result
    else:
        return {key: value}


def _flatten_item_for_merge(item: dict, prefix: str, level: int, merge_level: int) -> dict:
    """Flatten nested dict/list values into top-level keys, up to merge_level depth."""
    result = {}
    for k, v in item.items():
        key = f"{prefix}_{k}" if prefix else k
        result.update(_flatten_value(v, key, level, merge_level))
    return result


def read_output_yaml_list(
    run_dir: str, filename: str, list_key: str, merge_level: int = 0
) -> pd.DataFrame:
    """
    Read a YAML file from a run directory where the specified key contains a list
    of records (e.g. feedback messages with feedback, goal_id, timestamp per item).

    Args:
        run_dir: Path to the run directory
        filename: Name of the YAML file
        list_key: Key that holds the list (e.g. "feedback")
        merge_level: How many levels of nested dicts/lists to flatten into columns.
            0 = keep as-is. 1 = flatten one level (e.g. feedback dict becomes
            feedback_current_pose, feedback_distance_remaining, ...). Lists
            become key_0, key_1, etc. 2 = flatten two levels, etc.

    Returns:
        DataFrame with one row per list item, columns from each item's keys
    """
    yaml_path = os.path.join(run_dir, filename)
    if not os.path.exists(yaml_path):
        raise FileNotFoundError(f"{filename} not found in {run_dir}")
    with open(yaml_path, 'r') as f:
        yaml_data = yaml.safe_load(f)
    items = yaml_data[list_key]
    if not isinstance(items, list):
        raise ValueError(f"Key '{list_key}' does not contain a list, got {type(items)}")
    if merge_level > 0:
        items = [_flatten_item_for_merge(item, "", 0, merge_level) for item in items]
    return pd.DataFrame(items)


def _get_failure_text(root) -> str:
    """Get failure element text from a parsed test.xml root element."""
    for testcase in root.iter('testcase'):
        failure = testcase.find('failure')
        if failure is not None:
            return failure.text or failure.get('message', '') or ''
    return ''


def _extract_failure_summary(failure_text: str) -> Optional[str]:
    """Extract a short summary from a failure message.

    Algorithm: last '[✕] -- ' -> text after on that line;
    else last '[✓] -- ' -> text after on that line;
    if single-line message and no marker found -> take it completely.
    """
    if not failure_text:
        return None
    for marker in ("[✕] -- ", "[✓] -- "):
        idx = failure_text.rfind(marker)
        if idx >= 0:
            start = idx + len(marker)
            end = failure_text.find("\n", start)
            rest = failure_text[start:end] if end >= 0 else failure_text[start:]
            s = rest.strip()
            return s if s else None
    if "\n" not in failure_text:
        s = failure_text.strip()
        return s if s else None
    return None


def get_run_status(run_dir: Union[str, Path]) -> tuple:
    """Get the pass/fail status of a single run from its test.xml file.

    Args:
        run_dir: Path to the run directory (must contain a ``test.xml`` file).

    Returns:
        A ``(status, summary)`` tuple where *status* is ``'passed'``,
        ``'failed'``, or ``'unknown'``, and *summary* is a short descriptive
        string taken from the failure message (or ``None`` when not applicable).
    """
    run_xml_path = Path(run_dir) / "test.xml"
    if not run_xml_path.exists():
        return "unknown", None
    try:
        tree = ET.parse(run_xml_path)
        root = tree.getroot()
        testsuite = root if root.tag == 'testsuite' else root.find('testsuite')
        if testsuite is not None:
            errors = int(testsuite.get('errors', 0))
            failures = int(testsuite.get('failures', 0))
            status = "passed" if (errors == 0 and failures == 0) else "failed"
            summary = None
            if status == "failed":
                summary = _extract_failure_summary(_get_failure_text(root))
            return status, summary
    except Exception:
        pass
    return "unknown", None


def read_run_statuses(data_dir: str) -> pd.DataFrame:
    """Read pass/fail status for every run in *data_dir*.

    Iterates the same ``test.xml``-based run directories used by
    :func:`read_output_files` and returns a tidy DataFrame.

    Args:
        data_dir: Path to the results directory (campaign root or config root).

    Returns:
        DataFrame with columns ``run``, ``config``, ``status``
        (``'passed'`` | ``'failed'`` | ``'unknown'``), and ``summary``
        (short failure description or ``None``).
    """
    data_path = Path(data_dir)
    if not data_path.exists():
        raise ValueError(f"Data directory does not exist: {data_dir}")

    rows = []
    for run_xml in sorted(data_path.rglob("test.xml")):
        run_dir = run_xml.parent
        status, summary = get_run_status(run_dir)
        rows.append({
            'run': run_dir.name,
            'config': os.path.basename(run_dir.parent),
            'status': status,
            'summary': summary,
        })

    return pd.DataFrame(rows)


def for_each_run(data_dir: str, func: Callable[[Path], None], debug=False) -> None:
    """
    Applies a given function to each run directory within data_dir.

    Args:
        data_dir (str): Path to the directory containing run subdirectories.
        func (Callable[[Path], None]): Function to apply to each run directory.
        debug (bool, optional): If True, prints debug information. Defaults to False.

    Raises:
        ValueError: If data_dir does not exist or no test.xml files are found.
    """
    data_path = Path(data_dir)

    if not data_path.exists():
        raise ValueError(f"Data directory does not exist: {data_dir}")

    # Find all test.xml files in subdirectories
    run_xml_files = list(data_path.rglob("test.xml"))

    if not run_xml_files:
        raise ValueError(f"No test.xml files found in subdirectories of {data_dir}")

    if debug:
        print(f"Found {len(run_xml_files)} run directories")

    for run_xml in run_xml_files:
        run_dir = run_xml.parent
        if debug:
            print(f"Applying function to: {run_dir}")
        try:
            func(run_dir)
        except Exception as e:
            print(f"Warning: Could not apply function to {run_dir}: {e}")
            continue
