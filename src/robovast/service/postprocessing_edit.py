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

"""Editable, re-runnable per-campaign postprocessing.

Analysis postprocessing (and the run-view ``visualization`` panels) are *config*,
not captured data: the raw rosbags are the ground truth and are never touched, so
these blocks can be adapted and re-run any number of times to compute different
metrics. Editing therefore **overwrites the campaign's own ``_config/<name>.vast``
in place** — only the ``results_processing.postprocessing`` / ``visualization``
blocks; the as-ran ``configuration``/``execution`` are left as they are. There is no
override file and no revision history: the campaign carries exactly one ``.vast``.

Pure helpers here (no MCP/HTTP) so both the CLI and the service reuse them.
"""

import logging
from pathlib import Path

import yaml

# The one resolver for "this campaign's .vast" — shared with the cluster conversion
# Job (postprocess_job) and the rest of the service, so there is a single source of
# truth for which file is the campaign's config.
from robovast.common.results_utils import campaign_vast

logger = logging.getLogger(__name__)


def _load(vast_path: Path) -> dict:
    return yaml.safe_load(vast_path.read_text(encoding="utf-8")) or {}


def _write(vast_path: Path, data: dict) -> None:
    vast_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    logger.info("Updated campaign config %s", vast_path)


def get_postprocessing(campaign_dir: Path) -> dict:
    """Return the campaign's ``results_processing.postprocessing`` entries."""
    vast_path = campaign_vast(campaign_dir)
    data = _load(vast_path)
    entries = (data.get("results_processing") or {}).get("postprocessing", [])
    return {"campaign_dir": str(campaign_dir), "entries": entries}


def update_postprocessing(campaign_dir: Path, entries: list) -> dict:
    """Overwrite the campaign's ``results_processing.postprocessing`` list in place.

    Loads ``_config/<name>.vast``, replaces its ``results_processing.postprocessing``
    list (validated first) and writes the same file back — every other block is
    preserved, and the raw rosbags are untouched.
    """
    from robovast.common.config_validation import _postprocessing_problems

    if not isinstance(entries, list):
        raise ValueError("entries must be a list of postprocessing commands")

    problems = _postprocessing_problems(entries, str(Path(campaign_dir) / "_config"),
                                        "results_processing.postprocessing")
    if problems:
        raise ValueError("invalid postprocessing entries: " +
                         "; ".join(p["message"] for p in problems))

    vast_path = campaign_vast(campaign_dir)
    data = _load(vast_path)
    section = data.get("results_processing")
    if not isinstance(section, dict):
        section = {}
        data["results_processing"] = section
    section["postprocessing"] = entries
    _write(vast_path, data)
    return {"campaign_dir": str(campaign_dir), "entries": entries}


# -- postprocessing as editable YAML text (webui rerun dialog) ---------------
#
# The structured ``get_postprocessing``/``update_postprocessing`` above are the
# programmatic API (MCP tools, CLI). The webui edits the same block as YAML text
# in a Monaco editor — exactly like the visualization editor below — so these two
# wrap the structured helpers with the text (de)serialization the editor needs.


def get_postprocessing_source(campaign_dir: Path) -> dict:
    """Return the ``results_processing.postprocessing`` block as YAML text."""
    info = get_postprocessing(campaign_dir)
    content = yaml.safe_dump(
        {"results_processing": {"postprocessing": info["entries"]}},
        sort_keys=False)
    return {"campaign_dir": str(campaign_dir), "content": content}


def update_postprocessing_source(campaign_dir: Path, content: str) -> dict:
    """Overwrite the postprocessing block from an edited YAML document.

    *content* is the document as shown by :func:`get_postprocessing_source`: a
    top-level ``results_processing:`` mapping carrying a ``postprocessing:`` list.
    The entries are validated and written via :func:`update_postprocessing`, so only
    the ``postprocessing`` sub-key is replaced (siblings are preserved).
    """
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
    if not isinstance(parsed, dict) or "results_processing" not in parsed:
        raise ValueError("expected a top-level 'results_processing:' mapping")
    section = parsed["results_processing"]
    if not isinstance(section, dict) or "postprocessing" not in section:
        raise ValueError("expected a 'postprocessing:' list under 'results_processing:'")
    return update_postprocessing(campaign_dir, section["postprocessing"])


# -- run-view visualization (same in-place edit, display-only) ---------------
#
# The run view's panels come from the top-level ``visualization:`` block. Editing
# it is a pure display concern, so — like postprocessing — the edit overwrites the
# ``visualization`` key of ``_config/<name>.vast`` in place.


def get_visualization(campaign_dir: Path) -> dict:
    """Return the campaign's ``visualization:`` block as editable YAML text."""
    data = _load(campaign_vast(campaign_dir))
    section = data.get("visualization") or {}
    return {"campaign_dir": str(campaign_dir),
            "content": yaml.safe_dump({"visualization": section}, sort_keys=False)}


def update_visualization(campaign_dir: Path, content: str) -> dict:
    """Overwrite the ``visualization:`` block of ``_config/<name>.vast`` in place."""
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
    if not isinstance(parsed, dict) or "visualization" not in parsed:
        raise ValueError("expected a top-level 'visualization:' mapping")
    section = parsed["visualization"]
    if not isinstance(section, dict):
        raise ValueError("'visualization' must be a mapping")

    vast_path = campaign_vast(campaign_dir)
    data = _load(vast_path)
    data["visualization"] = section
    _write(vast_path, data)
    return {"campaign_dir": str(campaign_dir)}
