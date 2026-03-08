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

"""Zip publication plugin for RoboVAST.

Creates a zip archive for each campaign directory in the results directory.
Multiple zip entries may be defined to split a campaign into different archives
with different file selections.

Configuration format:

.. code-block:: yaml

   results_processing:
     publication:
       - zip:
           filename: my_dataset_{robot_id}_{timestamp}.zip
           exclude_filter:
           - "*.pyc"
           include_filter:
           - "*.csv"
           - "/_config/*"
           destination: archives/
           overwrite: true
"""

import datetime
import fnmatch
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from robovast.results_processing.publication_plugins.base import \
    BasePublicationPlugin


class _FormattableTimestamp(str):
    """A ``str`` subclass that supports datetime format specs.

    When used in a ``format_map`` call:

    * ``{timestamp}``            → original string, e.g. ``2026-03-07-224410``
    * ``{timestamp:%Y-%m-%d}``  → ``2026-03-07``
    * ``{timestamp:%Y%m%d}``    → ``20260307``
    """

    _PARSE_FORMAT = "%Y-%m-%d-%H%M%S"

    def __format__(self, format_spec: str) -> str:  # type: ignore[override]
        if not format_spec:
            return str(self)
        try:
            dt = datetime.datetime.strptime(str(self), self._PARSE_FORMAT)
            return dt.strftime(format_spec)
        except ValueError:
            # Fall back to the raw string if parsing fails.
            return str(self)


def _file_matches_pattern(rel_path: str, pattern: str) -> bool:
    """Test whether a relative file path matches a filter pattern.

    Patterns starting with ``/`` are anchored to the campaign root and matched
    against the full relative path.  Patterns containing ``/`` (but not leading)
    are matched against the full relative path.  Patterns without ``/`` are
    matched against the file's basename only.

    Args:
        rel_path: File path relative to the campaign directory (forward slashes).
        pattern: Glob pattern, optionally anchored with a leading ``/``.

    Returns:
        True if the pattern matches.
    """
    if pattern.startswith('/'):
        return fnmatch.fnmatch(rel_path, pattern.lstrip('/'))
    if '/' in pattern:
        return fnmatch.fnmatch(rel_path, pattern)
    return fnmatch.fnmatch(Path(rel_path).name, pattern)


def _should_include_file(
    rel_path: str,
    include_filter: Optional[List[str]],
    exclude_filter: Optional[List[str]],
) -> bool:
    """Decide whether to include a file in the archive.

    Inclusion logic:
    1. If ``include_filter`` is defined, only files matching at least one
       include pattern are considered.
    2. Files matching any ``exclude_filter`` pattern are always excluded.

    Args:
        rel_path: File path relative to campaign root (forward slashes).
        include_filter: Optional list of glob patterns; if set, only matching
            files are included.
        exclude_filter: Optional list of glob patterns; matching files are
            excluded regardless of include_filter.

    Returns:
        True if the file should be included.
    """
    # Check include filter first
    if include_filter:
        if not any(_file_matches_pattern(rel_path, p) for p in include_filter):
            return False

    # Check exclude filter
    if exclude_filter:
        if any(_file_matches_pattern(rel_path, p) for p in exclude_filter):
            return False

    return True


def _load_vast_metadata(vast_path: str) -> Dict[str, Any]:
    """Return the ``metadata`` section from a .vast file path.

    Returns an empty dict if the file cannot be read or ``metadata`` is absent.
    """
    try:
        with open(vast_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            return data.get("metadata") or {}
    except Exception:  # pylint: disable=broad-except
        pass
    return {}


def _resolve_filename(template: str, campaign_name: str, vast_metadata: Dict[str, Any]) -> str:
    """Expand ``{key}`` placeholders in *template* for a single campaign.

    Available substitution keys:

    * ``timestamp`` – the timestamp portion of the campaign directory name
      (e.g. ``campaign-2026-03-05-121530`` → ``2026-03-05-121530``).
    * Any key present in the vast file's top-level ``metadata`` section.

    Args:
        template: Filename template, e.g. ``my_dataset_{robot_id}_{timestamp}.zip``.
        campaign_name: The ``campaign-<timestamp>`` directory name.
        vast_metadata: Dict loaded from ``metadata:`` in the .vast file.

    Returns:
        The resolved filename string.

    Raises:
        ValueError: If the template references placeholders not available in
            *vast_metadata* or as the built-in ``timestamp`` key.
    """
    timestamp = (
        campaign_name[len("campaign-"):]
        if campaign_name.startswith("campaign-")
        else campaign_name
    )

    substitutions: Dict[str, Any] = {"timestamp": _FormattableTimestamp(timestamp)}
    substitutions.update(vast_metadata)

    # Match both plain {key} and formatted {key:spec} placeholders.
    keys_in_template = re.findall(r"\{(\w+)[^}]*\}", template)
    missing = [k for k in keys_in_template if k not in substitutions]
    if missing:
        available = sorted(substitutions.keys())
        raise ValueError(
            f"Filename template '{template}' references unknown placeholder(s): "
            f"{', '.join(missing)}. "
            f"Available: {', '.join(available) if available else 'none'}."
        )

    return template.format_map(substitutions)


class Zip(BasePublicationPlugin):
    """Create a zip archive for each campaign directory.

    For each ``campaign-*`` directory found under *results_dir*, a zip file is
    created containing the campaign's files.  Use ``include_filter`` to select
    only specific files and ``exclude_filter`` to skip files that would
    otherwise be included.

    Multiple ``zip`` entries may be defined in the publication configuration to
    produce different archives from the same campaign (e.g. one archive for CSV
    files and another for videos).

    When ``omit_hidden`` is ``True``, directory components whose names start
    with ``'_'`` are stripped from the file paths inside the archive.  For
    example a file stored on disk as
    ``campaign-2026-03-05-163338/_config/my_file.yaml`` is stored in the zip
    as ``campaign-2026-03-05-163338/my_file.yaml``.

    Configuration example:

    .. code-block:: yaml

       results_processing:
         publication:
           - zip:
               filename: vast-{robot_id}_{timestamp}.zip
               include_filter:
               - "_config/*"
               omit_hidden: true
    """

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        exclude_filter: Optional[List[str]] = None,
        include_filter: Optional[List[str]] = None,
        destination: Optional[str] = None,
        filename: Optional[str] = None,
        overwrite: Optional[bool] = None,
        omit_hidden: bool = False,
        _vast_file: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Create a zip archive for each campaign directory.

        Args:
            results_dir: Path to the results directory (parent of campaign-* dirs).
            config_dir: Directory containing the .vast config file; relative
                *destination* paths are resolved from here.
            exclude_filter: Glob patterns for files to exclude.  Patterns without
                a ``/`` match on the basename; patterns starting with ``/`` are
                anchored to the campaign root; other patterns containing ``/`` are
                matched against the full relative path.
            include_filter: Glob patterns for files to include.  If defined, only
                matching files are included (before applying *exclude_filter*).
                If omitted, all files are candidates.
            destination: Directory where zip files are written.  Relative paths are
                resolved from *results_dir*.  If not set, zip files are placed next
                to the campaign directories (i.e. directly inside *results_dir*).
            filename: Optional template for the zip filename, e.g.
                ``my_dataset_{robot_id}_{timestamp}.zip``.  Supported placeholders:

                * ``{timestamp}`` – timestamp extracted from the campaign directory
                  name (``campaign-<timestamp>``).
                * ``{<key>}`` – any key from the ``metadata:`` section of the
                  .vast file (e.g. ``{robot_id}``, ``{config_id}``).

                If omitted the default name ``<campaign-dir-name>.zip`` is used.
                A ``ValueError`` is raised listing available placeholders when an
                unknown placeholder is referenced.
            overwrite: Controls behaviour when a zip file already exists.

                * ``None`` (default) – prompt the user interactively; the default
                  answer is *yes* (overwrite).
                * ``True`` – silently overwrite existing files.
                * ``False`` – silently skip existing files.

                When running non-interactively (e.g. with ``--force`` on the CLI)
                this is automatically set to ``True``.
            omit_hidden: When ``True``, directory components starting with
                ``'_'`` are stripped from the archive member paths.  This means
                a file at ``_config/my_file.yaml`` is stored as ``my_file.yaml``
                inside the zip (relative to the campaign directory).  Defaults
                to ``False``.
            _vast_file: Internal – absolute path to the .vast file, injected by
                the publication runner.  Used to load ``metadata:`` for filename
                template substitution.  Not intended for manual configuration.

        Returns:
            Tuple of (success, message).

        Example usage in .vast config:

        .. code-block:: yaml

           results_processing:
             publication:
               - zip:
                   filename: my_dataset_{robot_id}_{timestamp}.zip
                   exclude_filter:
                   - "*.pyc"
                   include_filter:
                   - "*.csv"
                   - "_config/*"
                   omit_hidden: true
                   overwrite: true
                   destination: archives/
        """
        # Resolve destination directory relative to results_dir
        if destination:
            dest_dir = (
                Path(destination)
                if os.path.isabs(destination)
                else Path(results_dir) / destination
            )
        else:
            dest_dir = Path(results_dir)

        dest_dir = dest_dir.resolve()
        results_path = Path(results_dir).resolve()

        # Load vast metadata once (needed only when a filename template is provided)
        vast_metadata: Dict[str, Any] = (
            _load_vast_metadata(_vast_file) if (filename and _vast_file) else {}
        )

        created = []
        for campaign_item in sorted(results_path.iterdir()):
            if not campaign_item.is_dir() or not campaign_item.name.startswith("campaign-"):
                continue

            if filename:
                try:
                    resolved_name = _resolve_filename(filename, campaign_item.name, vast_metadata)
                except ValueError as exc:
                    return False, str(exc)
                zip_path = dest_dir / resolved_name
            else:
                zip_path = dest_dir / f"{campaign_item.name}.zip"

            # Handle existing zip file
            if zip_path.exists():
                if overwrite is True:
                    pass  # silently overwrite
                elif overwrite is False:
                    continue  # silently skip
                else:
                    # Prompt the user; default answer is Y (overwrite)
                    try:
                        answer = input(f"File already exists: {zip_path}  Overwrite? [Y/n] ").strip().lower()
                    except EOFError:
                        answer = ""
                    if answer not in ("", "y", "yes"):
                        continue

            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for entry in sorted(campaign_item.rglob("*")):
                        if not entry.is_file():
                            continue
                        rel = entry.relative_to(campaign_item)
                        rel_str = str(rel).replace(os.sep, "/")
                        if not _should_include_file(rel_str, include_filter, exclude_filter):
                            continue
                        member_path = self.get_arcname(rel_str, omit_hidden)
                        arcname = f"{campaign_item.name}/{member_path}"

                        # Get file modification time and clamp to ZIP-supported range (1980-2107)
                        mtime = os.path.getmtime(entry)
                        # Minimum ZIP timestamp: January 1, 1980 00:00:00 (315532800 in epoch time)
                        min_timestamp = 315532800
                        clamped_mtime = max(mtime, min_timestamp)

                        # Create ZipInfo with clamped timestamp
                        zinfo = zipfile.ZipInfo(filename=arcname, date_time=time.gmtime(clamped_mtime)[:6])
                        zinfo.external_attr = os.stat(entry).st_mode << 16

                        with open(entry, 'rb') as f:
                            zf.writestr(zinfo, f.read(), compress_type=zipfile.ZIP_DEFLATED)
                created.append(zip_path.name)
            except OSError as e:
                return False, f"Failed to create {zip_path}: {e}"

        if not created:
            return True, "No campaign-* directories found"
        return True, f"Created zip archives: {', '.join(created)}"
