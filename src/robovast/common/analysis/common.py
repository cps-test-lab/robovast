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
from pathlib import Path
from typing import Callable

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
    config_path = Path(data_dir) / "scenario.config"
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
    Reads and combines output data from all test subdirectories within data_dir.

    Args:
        data_dir (str): Path to the directory containing test subdirectories.
        reader_func (Callable[[Path], pd.DataFrame]): Function that reads test data from a directory and returns a DataFrame.
        debug (bool, optional): If True, prints debug information. Defaults to False.

    Returns:
        pd.DataFrame: Combined DataFrame containing all test data, with additional columns for test, config, and scenario parameters.

    Raises:
        ValueError: If data_dir does not exist, no run.yaml files are found, or no valid test data could be read.
    """
    data_path = Path(data_dir)

    if not data_path.exists():
        raise ValueError(f"Data directory does not exist: {data_dir}")

    all_dataframes = []

    # Find all run.yaml files in subdirectories
    run_yaml_files = list(data_path.rglob("run.yaml"))

    if not run_yaml_files:
        raise ValueError(f"No run.yaml files found in subdirectories of {data_dir}")

    if debug:
        print(f"Found {len(run_yaml_files)} test directories")

    category_names = set({'test', 'config'})
    for run_yaml in run_yaml_files:
        if debug:
            print(f"Reading data from: {run_yaml}")
        test_dir = run_yaml.parent
        test_name = test_dir.name

        try:
            # Call the user-provided reader function
            if reader_func:
                df = reader_func(test_dir)
            else:
                df = pd.DataFrame()
            scenario_config_path = run_yaml.parent / "scenario.config"
            config_parameters = {}
            try:
                with open(scenario_config_path, 'r') as f:
                    config_content = yaml.safe_load(f)

                    # skip scenario-name
                    if isinstance(config_content, dict) and len(config_content) == 1:
                        config_parameters = next(iter(config_content.values()))
            except Exception as e:
                print(f"Could not read scenario.config: {e}\n")

            df['test'] = str(test_name)
            df['config'] = str(os.path.basename(test_dir.parent))
            category_names.update(config_parameters.keys())
            for param_name, param_value in config_parameters.items():
                if isinstance(param_value, (dict, list)):
                    df[param_name] = yaml.safe_dump(param_value)
                else:
                    df[param_name] = param_value

            all_dataframes.append(df)

        except Exception as e:
            print(f"Warning: Could not read data from {test_dir}: {e}")
            continue

    if not all_dataframes:
        raise ValueError(f"No valid test data could be read from {data_dir}")

    # Combine all dataframes
    combined_df = pd.concat(all_dataframes, ignore_index=True)

    for category in category_names:
        combined_df[category] = combined_df[category].astype('category')

    if debug:
        print(f"Combined dataframe shape: {combined_df.shape}")
        print(f"Columns: {list(combined_df.columns)}")
        print(f"Number of unique tests: {combined_df['test'].nunique()}")

    return combined_df


def read_output_csv(test_dir: Path, filename: str, skiprows: int = 0) -> pd.DataFrame:
    """
    Read a CSV file from a test directory, skipping the first line (comment).

    Args:
        test_dir: Path to the test directory
        filename: Name of the CSV file to read

    Returns:
        DataFrame with the CSV data
    """
    csv_path = test_dir / filename
    if not csv_path.exists():
        raise FileNotFoundError(f"{filename} not found in {test_dir}")

    # Read CSV, skipping the first line (comment)
    df = pd.read_csv(csv_path, skiprows=skiprows)
    return df


def for_each_test(data_dir: str, func: Callable[[Path], None], debug=False) -> None:
    """
    Applies a given function to each test directory within data_dir.

    Args:
        data_dir (str): Path to the directory containing test subdirectories.
        func (Callable[[Path], None]): Function to apply to each test directory.
        debug (bool, optional): If True, prints debug information. Defaults to False.

    Raises:
        ValueError: If data_dir does not exist or no run.yaml files are found.
    """
    data_path = Path(data_dir)

    if not data_path.exists():
        raise ValueError(f"Data directory does not exist: {data_dir}")

    # Find all run.yaml files in subdirectories
    run_yaml_files = list(data_path.rglob("run.yaml"))

    if not run_yaml_files:
        raise ValueError(f"No run.yaml files found in subdirectories of {data_dir}")

    if debug:
        print(f"Found {len(run_yaml_files)} test directories")

    for run_yaml in run_yaml_files:
        test_dir = run_yaml.parent
        if debug:
            print(f"Applying function to: {test_dir}")
        try:
            func(test_dir)
        except Exception as e:
            print(f"Warning: Could not apply function to {test_dir}: {e}")
            continue
