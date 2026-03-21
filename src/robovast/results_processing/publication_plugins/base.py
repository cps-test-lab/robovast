# Copyright (C) 2026 Frederik Pasch
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

"""Base class for publication plugins."""

from pathlib import PurePosixPath
from typing import List, Tuple


class BasePublicationPlugin:
    """Base class for class-based publication plugins.

    Publication plugins come in two flavours controlled by :attr:`plugin_type`:

    * ``"packaging"`` – produces output files (e.g. zip archives).  Return their
      absolute paths as the third element of the return tuple so that subsequent
      upload plugins can consume them via ``_artifacts``.
    * ``"upload"`` – consumes artifacts from preceding packaging plugins.  The
      runner injects those paths via ``_artifacts``.  Upload plugins are skipped
      when ``--skip-upload`` is passed to ``vast results publish``.

    Subclasses must implement :meth:`__call__` with the standard publication
    plugin signature.  The base class also provides :meth:`get_arcname` for
    computing archive member paths, which supports stripping hidden (underscore-
    prefixed) directory components when ``omit_hidden`` is enabled.
    """

    #: Plugin role – either ``"packaging"`` or ``"upload"``.
    plugin_type: str = "packaging"

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        **kwargs,
    ) -> Tuple[bool, str, List[str]]:
        """Execute the publication plugin.

        Args:
            results_dir: Path to the results directory (parent of campaign-* dirs).
            config_dir: Directory containing the .vast config file.
            **kwargs: Plugin-specific keyword arguments from the config.

        Returns:
            Tuple of ``(success, message, artifacts)`` where *artifacts* is a list
            of absolute file paths produced by the plugin (e.g. zip archives).
            Returning a 2-tuple ``(success, message)`` is also accepted for
            backward compatibility.
        """
        raise NotImplementedError("Subclasses must implement __call__.")

    def get_arcname(self, rel_path: str, omit_hidden: bool = False) -> str:
        """Compute the archive member name for a file path.

        When *omit_hidden* is ``True``, directory components whose name starts
        with ``'_'`` are removed from the path.  The filename component is
        always kept unchanged.

        Args:
            rel_path: File path relative to the campaign directory, using
                forward slashes, e.g. ``"_config/files/my_file.yaml"``.
            omit_hidden: When ``True``, strip directory components starting
                with ``'_'`` from *rel_path*.

        Returns:
            The (possibly modified) path string to use as the archive member
            name (without the ``campaign-<id>/`` prefix).

        Examples::

            plugin.get_arcname("_config/my_file.yaml", omit_hidden=True)
            # returns "my_file.yaml"

            plugin.get_arcname("_config/subdir/my_file.yaml", omit_hidden=True)
            # returns "subdir/my_file.yaml"

            plugin.get_arcname("run-001/data.csv", omit_hidden=False)
            # returns "run-001/data.csv"
        """
        if not omit_hidden:
            return rel_path

        parts = list(PurePosixPath(rel_path).parts)
        if len(parts) <= 1:
            # Only a filename, no directory components to strip
            return rel_path

        filename = parts[-1]
        dir_parts = [p for p in parts[:-1] if not p.startswith('_')]
        if dir_parts:
            return '/'.join(dir_parts + [filename])
        return filename
