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
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_POSTPROCESS_SUBDIR = "_control/postprocess"
_REV_RE = re.compile(r"^rev-(\d+)\.vast$")


def _config_vast(campaign_dir: Path) -> Path:
    """The immutable snapshot ``.vast`` in ``_config/`` (never modified)."""
    config_dir = campaign_dir / "_config"
    vasts = sorted(config_dir.glob("*.vast"))
    if not vasts:
        raise ValueError(f"no .vast snapshot in {config_dir}")
    return vasts[0]


def _rev_dir(campaign_dir: Path) -> Path:
    return campaign_dir / _POSTPROCESS_SUBDIR


def _revs(campaign_dir: Path) -> list[tuple[int, Path]]:
    """All override revisions as ``(n, path)``, ascending."""
    d = _rev_dir(campaign_dir)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        m = _REV_RE.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def effective_vast(campaign_dir: Path) -> Path:
    """The `.vast` postprocessing currently uses: latest override, else snapshot."""
    revs = _revs(campaign_dir)
    return revs[-1][1] if revs else _config_vast(campaign_dir)


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

    rev_dir = _rev_dir(campaign_dir)
    rev_dir.mkdir(parents=True, exist_ok=True)
    next_n = (_revs(campaign_dir)[-1][0] + 1) if _revs(campaign_dir) else 1
    rev_path = rev_dir / f"rev-{next_n}.vast"
    rev_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    logger.info("Wrote postprocessing override %s", rev_path)
    return {"revision": next_n, "path": str(rev_path), "entries": entries}
