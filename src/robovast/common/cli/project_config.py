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

"""Project configuration management for VAST CLI."""

import json
import os
from pathlib import Path
from typing import Optional

import click

PROJECT_FILE = ".robovast_project"


class ProjectConfig:
    """Manages VAST project configuration stored in .vast_project file."""
    
    def __init__(self, config_path: Optional[str] = None, results_dir: Optional[str] = None):
        """Initialize project configuration.
        
        Args:
            config_path: Path to .vast configuration file
            results_dir: Directory for storing results
        """
        self.config_path = config_path
        self.results_dir = results_dir
    
    @classmethod
    def find_project_file(cls, start_dir: Optional[str] = None) -> Optional[str]:
        """Find .vast_project file by searching upward from start_dir.
        
        Args:
            start_dir: Directory to start searching from (defaults to current directory)
            
        Returns:
            Path to .vast_project file if found, None otherwise
        """
        if start_dir is None:
            start_dir = os.getcwd()
        
        current = Path(start_dir).resolve()
        
        # Search upward until we find .vast_project or reach root
        while True:
            project_file = current / PROJECT_FILE
            if project_file.exists():
                return str(project_file)
            
            parent = current.parent
            if parent == current:  # Reached root
                break
            current = parent
        
        return None
    
    @classmethod
    def load(cls, start_dir: Optional[str] = None) -> Optional['ProjectConfig']:
        """Load project configuration from .vast_project file.
        
        Args:
            start_dir: Directory to start searching from (defaults to current directory)
            
        Returns:
            ProjectConfig instance if found and valid, None otherwise
        """
        project_file = cls.find_project_file(start_dir)
        if not project_file:
            return None
        
        try:
            with open(project_file, 'r') as f:
                data = json.load(f)
            
            config_path = data.get('config')
            results_dir = data.get('results_dir')
            
            if not config_path or not results_dir:
                return None
            
            # Make paths absolute relative to project file location
            project_dir = os.path.dirname(project_file)
            if not os.path.isabs(config_path):
                config_path = os.path.abspath(os.path.join(project_dir, config_path))
            if not os.path.isabs(results_dir):
                results_dir = os.path.abspath(os.path.join(project_dir, results_dir))
            
            return cls(config_path=config_path, results_dir=results_dir)
        
        except (json.JSONDecodeError, IOError):
            return None
    
    def save(self, target_dir: Optional[str] = None) -> str:
        """Save project configuration to .vast_project file.
        
        Args:
            target_dir: Directory to save .vast_project file (defaults to current directory)
            
        Returns:
            Path to saved .vast_project file
        """
        if target_dir is None:
            target_dir = os.getcwd()
        
        project_file = os.path.join(target_dir, PROJECT_FILE)
        
        # Store paths relative to project file location if they're under target_dir
        target_path = Path(target_dir).resolve()
        
        config_to_save = self.config_path
        results_to_save = self.results_dir
        
        # Try to make paths relative for better portability
        try:
            config_path_obj = Path(self.config_path).resolve()
            if config_path_obj.is_relative_to(target_path):
                config_to_save = str(config_path_obj.relative_to(target_path))
        except (ValueError, AttributeError):
            pass
        
        try:
            results_path_obj = Path(self.results_dir).resolve()
            if results_path_obj.is_relative_to(target_path):
                results_to_save = str(results_path_obj.relative_to(target_path))
        except (ValueError, AttributeError):
            pass
        
        data = {
            'config': config_to_save,
            'results_dir': results_to_save
        }
        
        with open(project_file, 'w') as f:
            json.dump(data, f, indent=2)
        
        return project_file
    
    def validate(self) -> tuple[bool, Optional[str]]:
        """Validate that the configuration is valid.
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not self.config_path:
            return False, "Configuration path is not set"
        
        if not self.results_dir:
            return False, "Results directory is not set"
        
        if not os.path.exists(self.config_path):
            return False, f"Configuration file does not exist: {self.config_path}"
        
        if not os.path.isfile(self.config_path):
            return False, f"Configuration path is not a file: {self.config_path}"
        
        # Results directory doesn't need to exist yet, but its parent should
        results_parent = os.path.dirname(self.results_dir)
        if results_parent and not os.path.exists(results_parent):
            return False, f"Parent directory of results directory does not exist: {results_parent}"
        
        return True, None


def get_project_config() -> ProjectConfig:
    """Get project configuration or raise an error if not found.
    
    Returns:
        ProjectConfig instance
        
    Raises:
        click.ClickException: If project is not initialized or configuration is invalid
    """
    
    config = ProjectConfig.load()
    if not config:
        raise click.ClickException(
            "Project not initialized. Run 'vast init --config <config-file> --results-dir <dir>' first."
        )
    
    is_valid, error = config.validate()
    if not is_valid:
        raise click.ClickException(f"Invalid project configuration: {error}")
    
    return config
