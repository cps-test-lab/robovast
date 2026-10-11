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

"""Built-in postprocessing steps: run a script.

A campaign's tables are not a step: the decoder builds them from its records when something
names them (:mod:`robovast_data`). The steps here are what a campaign asks to *run* at its
end, beside any plugin it names by entry point or ``./path.py:Class``.

All plugins inherit from :class:`BasePostprocessingPlugin` and implement
:meth:`~BasePostprocessingPlugin.__call__`, which gets the campaign directory, the directory
of the ``.vast`` for resolving relative paths, and the entry's own parameters, and returns
``(success, message)``. A plugin may override :meth:`~BasePostprocessingPlugin.get_files_to_copy`
to have files it needs copied into ``_config/``.

Configuration format:
    postprocessing:
      - plugin_name:
          param1: value1
          param2: value2
      - simple_plugin_name
"""
import os
import subprocess
from typing import List, Optional, Tuple


class BasePostprocessingPlugin:
    """Base class for class-based postprocessing plugins.

    Subclasses must implement :meth:`__call__` with the standard plugin
    signature.  Override :meth:`get_files_to_copy` to declare additional files
    that should be copied into the campaign ``_config/`` directory before
    execution (e.g. helper scripts referenced by the plugin).
    """

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        **kwargs,
    ) -> Tuple[bool, str]:
        """Execute the postprocessing plugin.

        Args:
            results_dir: Path to the campaign-<id> directory to process.
            config_dir: Directory containing the .vast config file (used to
                resolve relative paths).
            **kwargs: Plugin-specific keyword arguments from the config.

        Returns:
            Tuple of (success, message).
        """
        raise NotImplementedError("Subclasses must implement __call__.")

    def get_files_to_copy(self, config_dir: str, params: dict) -> List[str]:
        """Return file paths (relative to *config_dir*) that must be copied.

        Override this method to declare additional files that the plugin needs
        at execution time.  The returned paths are relative to *config_dir* and
        will be copied into the campaign ``_config/`` directory so that they
        are available as ``_config/<path>`` wherever the campaign is postprocessed.

        Args:
            config_dir: Absolute path to the directory containing the .vast
                config file.
            params: The plugin parameters dict from the .vast config, i.e. the
                same keyword arguments that will be passed to :meth:`__call__`.

        Returns:
            List of relative file paths (relative to *config_dir*) to copy.
        """
        return []


class Command(BasePostprocessingPlugin):
    """Execute an arbitrary command or script.

    Generic plugin that allows execution of any command or script path.
    Use this for custom scripts or when a specific plugin doesn't exist.

    The script (when given as a relative path) is automatically copied into
    the campaign ``_config/`` directory, so a re-run of the campaign's postprocessing
    finds it wherever it runs.

    Example usage in .vast config:

    .. code-block:: yaml

        postprocessing:
          - command:
              script: postprocess.sh
          - command:
              script: analyse.py
              args: [--arg, value]
          - command:
              script: /absolute/path/to/script.sh
    """

    def get_files_to_copy(self, config_dir: str, params: dict) -> List[str]:
        """Return the script path if it is a relative path that exists.

        Args:
            config_dir: Directory containing the .vast config file.
            params: Plugin parameters, expected to contain ``script``.

        Returns:
            List with the relative script path when it resolves to an
            existing file; empty list otherwise.
        """
        script = params.get('script')
        if not script or os.path.isabs(script):
            return []
        candidate = os.path.join(config_dir, script)
        if os.path.isfile(candidate):
            return [script]
        return []

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        script: str,
        args: Optional[List[str]] = None,
        provenance_file: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Execute the configured script.

        Args:
            results_dir: Path to the campaign-<id> directory to process
            config_dir: Directory containing the config file (for resolving relative paths)
            script: Script path to execute (relative or absolute)
            args: Optional list of command-line arguments to pass to the script
            provenance_file: Optional path for provenance JSON (passed to script if it supports it)

        Returns:
            Tuple of (success, message)
        """
        # Resolve script path if not absolute
        script_path = script
        if not os.path.isabs(script_path) and config_dir:
            script_path = os.path.join(config_dir, script_path)

        if not os.path.exists(script_path):
            return False, f"Script not found: {script_path}"

        # A script can arrive non-executable: a workspace push from a filesystem or transport
        # that does not preserve POSIX modes lands it that way. Restored here, the one place
        # that execve()s the file -- one stat/chmod pair.

        if os.path.isfile(script_path) and not os.access(script_path, os.X_OK):
            try:
                mode = os.stat(script_path).st_mode
                os.chmod(script_path, mode | 0o111)
            except OSError:
                pass  # fall through; the exec below reports the real error if this didn't help

        # Build the full command, passing the provenance file to a script that takes one
        full_command = [script_path]
        if provenance_file:
            full_command.extend(["--provenance-file", provenance_file])
        if args:
            full_command.extend(args)
        full_command.append(results_dir)

        try:
            result = subprocess.run(
                full_command,
                cwd=results_dir,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'}
            )

            if result.returncode != 0:
                return False, f"Command failed with exit code {result.returncode}\n{result.stderr}"

            output = result.stdout.strip()
            return True, f"Command executed successfully\n{output}" if output else "Command executed successfully"

        except Exception as e:
            return False, f"Error executing command: {e}"
