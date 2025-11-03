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

"""Preprocessing functionality for analysis data."""

import os
from typing import List

from robovast.common import FileCache, load_config


def get_preprocessing_commands(config_path: str) -> List[str]:
    """Get preprocessing commands from configuration file.
    
    Args:
        config_path: Path to .vast configuration file
        
    Returns:
        List of preprocessing commands or empty list if none defined
    """
    try:
        analysis_config = load_config(config_path, subsection="analysis")
        return analysis_config.get("preprocessing", [])
    except (ValueError, KeyError):
        return []


def is_preprocessing_needed(config_path: str, results_dir: str) -> bool:
    """Check if preprocessing is needed.

    Args:
        config_path: Path to .vast configuration file
        results_dir: Path to the results directory

    Returns:
        bool indicating if preprocessing is needed
    """
    commands = get_preprocessing_commands(config_path)
    
    if not commands:
        return False
    
    command_files = []
    for command in commands:
        splitted = command.split()
        if splitted:
            if os.path.isabs(splitted[0]):
                command_path = splitted[0]
            else:
                command_path = os.path.join(os.path.dirname(config_path), splitted[0])
            if os.path.exists(command_path):
                command_files.append(command_path)

    cached_file = get_cached_file(os.path.dirname(config_path), results_dir, commands, command_files)

    return not bool(cached_file)

def get_hash_file_name(results_dir: str) -> str:
    file_name = "preprocess_" + results_dir
    return file_name.replace(os.sep, "_")

def get_cached_file(config_path, results_dir, commands, command_files):
    file_cache = FileCache()
    file_cache.set_current_data_directory(config_path)

    return file_cache.get_cached_file(command_files, get_hash_file_name(results_dir), content=False, strings_for_hash=commands, hash_only=True)
