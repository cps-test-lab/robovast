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

Analysis postprocessing is not privileged: the raw rosbags are always preserved,
so the ``results_processing.postprocessing`` entries can be edited and re-run any
number of times against the same data — to compute different metrics later.

The immutable ``_config/`` snapshot (what actually ran) is **never** mutated.
Edits are written as **versioned full-`.vast` overrides** under
``<campaign>/_control/postprocess/rev-N.vast``; the latest rev is the effective
config, and ``vast results postprocess --override <rev>`` reprocesses with it.

Pure helpers here (no MCP/HTTP) so both the CLI and the service reuse them.
"""

import logging
from pathlib import Path

import yaml

# The `.vast` resolver lives in common so the CLI, the service, and the in-cluster
# conversion Job all resolve the effective (override-aware) config identically.
from robovast.common.postprocess_config import (config_vast, effective_vast,
                                                rev_dir, revs)

logger = logging.getLogger(__name__)

# Back-compat aliases for the private names this module used before the resolver
# moved to common; kept so the rest of this file reads unchanged.
_config_vast = config_vast
_rev_dir = rev_dir
_revs = revs


def get_postprocessing(campaign_dir: Path) -> dict:
    """Return the effective postprocessing entries + revision history."""
    vast_path = effective_vast(campaign_dir)
    data = yaml.safe_load(vast_path.read_text(encoding="utf-8")) or {}
    entries = (data.get("results_processing") or {}).get("postprocessing", [])
    revs = _revs(campaign_dir)
    return {
        "campaign_dir": str(campaign_dir),
        "source": vast_path.name,
        "entries": entries,
        "revisions": [n for n, _ in revs],
    }


def update_postprocessing(campaign_dir: Path, entries: list) -> dict:
    """Write a new override revision with *entries* as the postprocessing list.

    Copies the current effective `.vast` in full, replaces its
    ``results_processing.postprocessing`` list, and writes ``rev-<N+1>.vast``.
    Validates the entries first; the ``_config/`` snapshot is untouched.
    """
    from robovast.common.config_validation import _postprocessing_problems

    if not isinstance(entries, list):
        raise ValueError("entries must be a list of postprocessing commands")

    base = effective_vast(campaign_dir)
    data = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
    problems = _postprocessing_problems(entries, str(campaign_dir / "_config"),
                                        "results_processing.postprocessing")
    if problems:
        raise ValueError("invalid postprocessing entries: " +
                         "; ".join(p["message"] for p in problems))

    data.setdefault("results_processing", {})
    if data["results_processing"] is None:
        data["results_processing"] = {}
    data["results_processing"]["postprocessing"] = entries

    return _write_override(campaign_dir, data, "postprocessing",
                           extra={"entries": entries})


# -- postprocessing as editable YAML text (webui rerun dialog) ---------------
#
# The structured ``get_postprocessing``/``update_postprocessing`` above are the
# programmatic API (MCP tools, CLI). The webui edits the same block as YAML text
# in a Monaco editor — exactly like the visualization editor below — so these two
# wrap the structured helpers with the text (de)serialization the editor needs.


def get_postprocessing_source(campaign_dir: Path) -> dict:
    """Return the effective ``results_processing.postprocessing`` block as YAML text."""
    info = get_postprocessing(campaign_dir)
    content = yaml.safe_dump(
        {"results_processing": {"postprocessing": info["entries"]}},
        sort_keys=False)
    return {
        "campaign_dir": str(campaign_dir),
        "source": info["source"],
        "content": content,
    }


def update_postprocessing_source(campaign_dir: Path, content: str) -> dict:
    """Write a new override revision from an edited postprocessing YAML document.

    *content* is the document as shown by :func:`get_postprocessing_source`: a
    top-level ``results_processing:`` mapping carrying a ``postprocessing:`` list.
    The entries are validated and persisted via :func:`update_postprocessing`, so
    the ``_config/`` snapshot is untouched and only the ``postprocessing`` sub-key
    is replaced (any siblings under ``results_processing`` are preserved).
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


# -- run-view visualization (same override chain, display-only) --------------
#
# The run view's panels come from the top-level ``visualization:`` block. Editing
# it is a pure display concern, so — like postprocessing — edits are written as
# full-`.vast` override revisions (never mutating ``_config/``) and picked up by
# ``effective_vast``.


def get_visualization(campaign_dir: Path) -> dict:
    """Return the effective ``visualization:`` block as editable YAML text."""
    vast_path = effective_vast(campaign_dir)
    data = yaml.safe_load(vast_path.read_text(encoding="utf-8")) or {}
    section = data.get("visualization") or {}
    return {
        "campaign_dir": str(campaign_dir),
        "source": vast_path.name,
        "content": yaml.safe_dump({"visualization": section}, sort_keys=False),
    }


def update_visualization(campaign_dir: Path, content: str) -> dict:
    """Write a new override revision replacing the ``visualization:`` block.

    *content* is the edited YAML document as shown by :func:`get_visualization`:
    a top-level ``visualization:`` mapping. Copies the current effective `.vast`
    and replaces its ``visualization`` key; the ``_config/`` snapshot is untouched.
    """
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
    if not isinstance(parsed, dict) or "visualization" not in parsed:
        raise ValueError("expected a top-level 'visualization:' mapping")
    section = parsed["visualization"]
    if not isinstance(section, dict):
        raise ValueError("'visualization' must be a mapping")

    base = effective_vast(campaign_dir)
    data = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
    data["visualization"] = section
    return _write_override(campaign_dir, data, "visualization")


def _write_override(campaign_dir: Path, data: dict, kind: str,
                    extra: dict | None = None) -> dict:
    """Write *data* as the next ``rev-N.vast`` override and return its number."""
    rev_dir = _rev_dir(campaign_dir)
    rev_dir.mkdir(parents=True, exist_ok=True)
    next_n = (_revs(campaign_dir)[-1][0] + 1) if _revs(campaign_dir) else 1
    rev_path = rev_dir / f"rev-{next_n}.vast"
    rev_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    logger.info("Wrote %s override %s", kind, rev_path)
    return {"revision": next_n, "path": str(rev_path), **(extra or {})}
