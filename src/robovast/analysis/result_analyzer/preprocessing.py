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

import hashlib
import os
from typing import List, Tuple

from robovast.common import load_config


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


def compute_preprocessing_hash(commands: List[str]) -> str:
    """Compute hash of preprocessing commands.
    
    Args:
        commands: List of preprocessing command strings
        
    Returns:
        Hash string of the commands
    """
    content = "\n".join(commands)
    return hashlib.sha256(content.encode()).hexdigest()


def get_flag_file_path(results_dir: str) -> str:
    """Get path to preprocessing flag file.
    
    Args:
        results_dir: Results directory path
        
    Returns:
        Path to .robovast_preprocessed flag file
    """
    return os.path.join(results_dir, ".robovast_preprocessed")


def is_preprocessing_needed(config_path: str, results_dir: str) -> Tuple[bool, str]:
    """Check if preprocessing is needed.
    
    Args:
        config_path: Path to .vast configuration file
        results_dir: Results directory path
        
    Returns:
        Tuple of (is_needed, reason) where is_needed is bool and reason is string
    """
    commands = get_preprocessing_commands(config_path)
    
    if not commands:
        return False, "No preprocessing commands defined"
    
    flag_file = get_flag_file_path(results_dir)
    
    if not os.path.exists(flag_file):
        return True, "Preprocessing has not been run yet"
    
    # Read existing hash
    try:
        with open(flag_file, 'r') as f:
            stored_hash = f.read().strip()
    except IOError:
        return True, "Cannot read preprocessing flag file"
    
    # Compute current hash
    current_hash = compute_preprocessing_hash(commands)
    
    if stored_hash != current_hash:
        return True, "Preprocessing commands have changed"
    
    return False, "Preprocessing is up to date"
