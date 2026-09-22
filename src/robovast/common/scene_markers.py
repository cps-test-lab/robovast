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

"""Collecting what each variation contributes to the config view.

The **vocabulary** -- :class:`SceneMarker`, :class:`ConfigViewContribution` -- is defined in
:mod:`robovast.client.scene_markers`, because it is also the shape the service serves and the
web UI's types are generated from that schema. It is re-exported here so a variation author
imports one module and never has to know which distribution the model lives in.

What is *here* is the part that needs the variation classes: asking each of them, in order,
what it contributes for one resolved configuration.
"""

from pathlib import Path
from typing import Any

from robovast.client.scene_markers import ConfigViewContribution, Point, SceneMarker

__all__ = ["ConfigViewContribution", "Point", "SceneMarker", "campaign_contribution",
           "collect_contributions", "contribution_for_block"]


def collect_contributions(config: dict, variation_classes, base_path: str) -> dict[str, Any]:
    """Ask every variation of one resolved *config* what it contributes.

    Returns the transport shape ``{markers, files, errors}``. A variation whose hook raises
    is reported in ``errors`` rather than dropped: a view that silently loses one
    variation's markers looks like a variation that placed nothing, which is the failure
    this repo's fail-loudly rule exists to prevent. The other variations still draw.
    """
    total = ConfigViewContribution()
    errors: list[str] = []
    for variation_class in variation_classes:
        name = getattr(variation_class, "__name__", str(variation_class))
        try:
            contributed = variation_class.config_view_data(config, base_path)
        except Exception as exc:  # noqa: BLE001 - one broken hook must not blank the view
            errors.append(f"{name}: {exc}")
            continue
        if contributed is None:
            continue
        # Default the group to the variation that produced it, so a view with two
        # populations can tell them apart without every plugin remembering to set it.
        for marker in contributed.markers:
            if not marker.group:
                marker.group = name
        total = total.merged_with(contributed)
    return {"markers": [m.model_dump(exclude_none=True) for m in total.markers],
            "files": total.files, "errors": errors}


def contribution_for_block(config: dict, block: dict, base_path: str) -> dict[str, Any]:
    """What the variations named by *block* contribute for *config*: ``{markers, files, errors}``.

    *block* is the ``.vast`` configuration block the config was composed from -- only its
    ``variations`` list is read. A variation type that cannot be resolved (an uninstalled
    plugin, a missing local file) is reported in ``errors`` rather than raised, and the
    view is then empty: raising would hide the markers the resolvable variations have.
    """
    from robovast.common.config_generation import \
        _get_variation_classes  # pylint: disable=import-outside-toplevel
    try:
        classes = [cls for cls, _params, _ref in _get_variation_classes(block or {}, base_path)]
    except Exception as exc:  # noqa: BLE001 - an unresolvable plugin is reported, not raised
        return {"markers": [], "files": {}, "errors": [f"variation types: {exc}"]}
    return collect_contributions(config, classes, base_path)


def campaign_contribution(campaign_dir, config_name: str) -> dict[str, Any]:
    """One configuration's contribution, derived from what the campaign froze.

    Derived on every call rather than stored: the contribution is a pure function of the
    resolved configuration (``_transient/configurations.yaml``) and the variation types that
    produced it, both of which the campaign already records. A stored copy would be a second
    source of the same fact.

    The variation types come from the block the configuration was composed from, found by the
    ``_config_name`` the composition recorded. A search campaign composes its blocks from
    ``search.variations`` rather than from ``configuration:``, so there that list is the block.
    ``files`` are returned campaign-relative (under ``_config/``, where the campaign keeps its
    workspace), so a caller addresses them as ``/results/<campaign_id>/<path>``.

    Raises ``KeyError`` when the campaign or the configuration is unknown, and ``ValueError``
    when the configuration names a block its ``.vast`` does not have.
    """
    from robovast.common.campaign_data import \
        read_resolved_configurations  # pylint: disable=import-outside-toplevel
    from robovast.common.config_validation import \
        _safe_load  # pylint: disable=import-outside-toplevel
    from robovast.common.results_utils import \
        vast_in_config_dir  # pylint: disable=import-outside-toplevel

    campaign_dir = Path(campaign_dir)
    config_dir = campaign_dir / "_config"
    try:
        resolved = read_resolved_configurations(campaign_dir) or {}
    except FileNotFoundError as exc:
        raise KeyError(f"campaign {campaign_dir.name!r} records no resolved configurations "
                       "(_transient/configurations.yaml)") from exc
    configs = {c.get("name"): c for c in resolved.get("configs") or [] if isinstance(c, dict)}
    config = configs.get(config_name)
    if config is None:
        raise KeyError(f"no config {config_name!r} in campaign {campaign_dir.name!r}; it has "
                       f"{', '.join(sorted(n for n in configs if n)) or 'none'}")
    vast = vast_in_config_dir(config_dir)
    if vast is None:
        raise KeyError(f"campaign {campaign_dir.name!r} has no .vast under _config/")
    authored, _ = _safe_load(str(vast))
    authored = authored or {}

    block_name = config.get("_config_name")
    blocks = {b.get("name"): b for b in authored.get("configuration") or []
              if isinstance(b, dict)}
    if block_name in blocks:
        block = blocks[block_name]
    elif authored.get("search") is not None:
        block = {"variations": (authored.get("search") or {}).get("variations") or []}
    else:
        raise ValueError(
            f"config {config_name!r} was composed from block {block_name!r}, which "
            f"{vast.name} does not have; its blocks are "
            f"{', '.join(sorted(n for n in blocks if n)) or 'none'}")

    contribution = contribution_for_block(config, block, str(config_dir))
    contribution["files"] = {role: path if path.startswith("/") else f"_config/{path}"
                             for role, path in (contribution.get("files") or {}).items()}
    return contribution
