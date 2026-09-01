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

"""Compose sampled parameter sets into runnable configs.

This is the bridge from search to the existing generation/packing/execution
path: each :class:`ParamSet` is turned into one ``configuration`` block, then the
existing ``generate_scenario_variations`` chain runs to produce
``campaign_data["configs"]`` — exactly the structure the packer and launchers
already consume. No rewrite of the variation plugins is required.

How a sampled value reaches a config:

* **Variation template** — the ``search:`` block may carry a ``variations:`` (and
  ``parameters:``) template, identical in shape to a batch ``configuration``
  block. It fixes most variation parameters inline and references the *searched*
  ones with a ``$name`` / ``${name}`` marker naming a ``search_space`` dimension.
  Compose deep-copies the template per param set and substitutes each marker with
  the sampled value (preserving its native type). This is disjoint from the
  ``@name`` *scenario-parameter* reference resolved inside the variation plugins.
* **Direct scenario parameter (fallback)** — any ``search_space`` dimension *not*
  referenced anywhere in the template is set directly as a scenario parameter
  (the simple-sweep case: no ``variations:`` ⇒ every dim is a scenario param).

A variation in the template must collapse to **exactly one** config per param set
(search relies on a 1:1 paramset→config mapping); Compose enforces this and
reports a clear error if a variation expands combinatorially.
"""

import copy
import logging
import os
import tempfile
from collections import defaultdict
from typing import Any

import yaml

from robovast.common.common import load_config
from robovast.common.config import match_var_marker
from robovast.common.config_generation import (_collect_analysis_input_files,
                                              _plugin_run_files,
                                              generate_scenario_variations)

from .types import ParamSet

logger = logging.getLogger(__name__)

# Composition progress (and the pip-install output the isolated-plugin compose
# subprocess forwards) is routed here — a child of "robovast" logged at INFO — so
# it reaches the campaign log's active phase file, exactly as the batch path does
# via ``run_batch_campaign``. Without this the composition falls back to
# ``generate_scenario_variations``'s ``logger.debug`` default, which the campaign
# log handler (gated at INFO) drops — leaving a plugin install silent.
variation_logger = logging.getLogger("robovast.variation")

#: A preview composes for real (the variation plugins run), so a campaign with a
#: large ``per_batch`` is sampled down rather than expanded in full.
_PREVIEW_SAMPLE_CAP = 8


def config_name_for(param_set: ParamSet) -> str:
    """Deterministic, schema-valid config/result-dir name for a param set.

    Prefixed with a letter so the name is always lowercase-cased (a pure-digit
    hash would fail the ``configuration.name`` validator).
    """
    return f"c{param_set.id}"


def distinct_draws(param_sets: list[ParamSet], where: str = "this batch") -> list[ParamSet]:
    """One entry per distinct cell of *param_sets*, keeping the first of each.

    A strategy may propose the same parameter values twice in one batch, and on a discrete
    space it routinely does: optuna's TPE re-proposes a category it likes, and a random or
    low-discrepancy draw collides as soon as the space has few enough levels. Two such
    draws are not two cells. :attr:`ParamSet.id` is derived from the values and everything
    downstream is addressed by it -- the config's name, its result directory, the unit row
    -- so the campaign has exactly one place to put their results.

    Composing both anyway produced two configs under one name. :meth:`Compose._resolve_names`
    saw a block that had yielded two configs, which is its signature for a variation that
    expanded combinatorially, and aborted the campaign telling the operator to make their
    variation parameters scalar -- on a campaign that may declare no variations at all.
    Had it passed, the two would have written into one result directory and been recorded
    as two units over one set of runs, counting every run of that cell twice.

    So the repeat is dropped **before** composition, and the strategy is told one
    evaluation for that cell -- the short generation :meth:`SearchStrategy.tell` already
    contracts to accept. What the strategy PROPOSED is not lost: the controller records it
    as ``batch.asked``, which is what a resume replays.
    """
    seen: dict[str, int] = {}
    distinct = []
    for ps in param_sets:
        if ps.id in seen:
            seen[ps.id] += 1
            continue
        seen[ps.id] = 1
        distinct.append(ps)
    repeated = {ps_id: n for ps_id, n in seen.items() if n > 1}
    if repeated:
        logger.warning(
            "%s proposed %d parameter set(s) but only %d distinct cell(s): %s. A repeat is "
            "the same cell -- its results are addressed by the same id -- so it is "
            "evaluated once and the strategy is told once for it. A space with few levels "
            "reaches this often; that is the space, not a fault.",
            where, len(param_sets), len(distinct),
            ", ".join(f"{ps_id} x{n}" for ps_id, n in sorted(repeated.items())))
    return distinct


def _set_scenario_param(params: list, name: str, value: Any) -> None:
    """Set scenario parameter ``name`` in a list of single-key dicts."""
    for entry in params:
        if isinstance(entry, dict) and name in entry:
            entry[name] = value
            return
    params.append({name: value})


def _substitute_vars(node: Any, values: dict[str, Any], used: set[str]) -> Any:
    """Deep-copy ``node`` replacing every ``$name`` / ``${name}`` marker leaf with
    ``values[name]`` (verbatim, so the value keeps its native type).

    Records each consumed variable name in ``used``. A leading ``$$`` is an
    escaped literal ``$``. Strings that are not whole-value markers (including
    ``@name`` references and file paths) pass through unchanged. Raises
    ``ValueError`` for a marker that names no declared variable.
    """
    if isinstance(node, dict):
        return {k: _substitute_vars(v, values, used) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_substitute_vars(v, values, used) for v in node]
    if isinstance(node, str):
        name = match_var_marker(node)
        if name is not None:
            if name not in values:
                raise ValueError(
                    f"variations template references '{node}', which is not a "
                    f"search_space variable; declared: {sorted(values)}")
            used.add(name)
            return copy.deepcopy(values[name])
        if node.startswith("$$"):
            return node[1:]  # collapse leading $$ to a literal $
        return node
    return node


class Compose:
    """Turns parameter sets into ``campaign_data`` using a base ``.vast``."""

    def __init__(self, vast_file: str, image_project: str | None = None,
                 image_project_tag: str | None = None):
        self.vast_file = os.path.abspath(vast_file)
        self.vast_dir = os.path.dirname(self.vast_file)
        # Which project the RoboVAST family images resolve from, for every batch this
        # composes. Held here rather than read at compose time because a search composes
        # repeatedly over the campaign's life, and all of those batches belong to the one
        # campaign that chose the project.
        self.image_project = image_project
        self.image_project_tag = image_project_tag
        self.base = load_config(self.vast_file)
        # The variation/parameter template lives in the search: block. Each param
        # set fills it in; unreferenced search dims fall back to scenario params.
        search = self.base.get("search") or {}
        self.variations_template = search.get("variations")
        self.fixed_parameters = search.get("parameters")

    def compose(self, param_sets: list[ParamSet], output_dir: str) -> tuple[dict, dict]:
        """Generate configs for ``param_sets``.

        Returns ``(campaign_data, name_by_id)`` where ``name_by_id`` maps each
        ``ParamSet.id`` to its config (result-dir) name.
        """
        ids = [ps.id for ps in param_sets]
        if len(set(ids)) != len(ids):
            # Stated here rather than discovered in `_resolve_names`, which sees only that
            # a block produced two configs and reports that as a combinatorial variation --
            # a diagnosis that sends the operator to variation parameters a campaign in this
            # state may not even declare. Callers collapse a repeat with `distinct_draws`;
            # reaching composition with one is a caller's bug, and this says whose.
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"compose() was given the same parameter set more than once "
                f"({', '.join(duplicates)}). A repeated draw is one cell -- results are "
                f"addressed by ParamSet.id -- so it cannot become two configs. Collapse "
                f"the batch with search.compose.distinct_draws() before composing it.")
        blocks = []
        id_by_block = {}
        for ps in param_sets:
            used: set[str] = set()
            block_name = config_name_for(ps)
            block: dict = {"name": block_name}
            if self.fixed_parameters is not None:
                block["parameters"] = _substitute_vars(
                    self.fixed_parameters, ps.values, used)
            if self.variations_template is not None:
                block["variations"] = _substitute_vars(
                    self.variations_template, ps.values, used)
            # Any search dim not consumed by the template is a direct scenario
            # parameter (the simple-sweep case, e.g. the quadrotor example).
            for key, value in ps.values.items():
                if key not in used:
                    _set_scenario_param(block.setdefault("parameters", []), key, value)
            blocks.append(block)
            id_by_block[block_name] = ps.id

        params = copy.deepcopy(self.base)
        params["configuration"] = blocks
        # Drop any closed-loop block so the generated temp campaign is a plain
        # batch of the composed configs.
        params.pop("search", None)

        # Write the temp .vast in the original dir so relative paths
        # (scenario_file, run_files, …) resolve identically.
        fd, temp_vast = tempfile.mkstemp(
            prefix=".robovast_search_", suffix=".vast", dir=self.vast_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(params, f, sort_keys=False)
            campaign_data = generate_scenario_variations(
                variation_file=temp_vast,
                output_dir=output_dir,
                use_cache=False,
                tolerate_infeasible=True,
                progress_update_callback=variation_logger.info,
                image_project=self.image_project,
                image_project_tag=self.image_project_tag,
            )
            # Repoint "vast" at the persistent original (same dir, so relative
            # scenario_file/run_files still resolve) so downstream consumers that
            # read or copy the file (e.g. prepare_campaign_configs) don't depend
            # on the temp file we are about to delete.
            campaign_data["vast"] = self.vast_file
            # And restore what the `search:` pop above dropped. Composition only ever saw a
            # temp `.vast` with no `search:` block -- it has to, since a config carrying both
            # `search:` and `configuration:` is refused -- so the strategy, the extractor and
            # the in-loop postprocessing modules were invisible to the collectors, and those
            # are exactly what the controller loads at the top of every batch. Corrected here
            # rather than passed into composition for the same reason the line above is: the
            # temp file misstates the campaign, and the caller is what knows the truth.
            #
            # After the call, not before, and that is safe: `hash_run_files` runs later, in
            # prepare_campaign_configs, off this same list -- so these still reach the config
            # identity -- and the only thing computed earlier is the composition cache key,
            # which this path never consults (use_cache=False above).
            for rel in _plugin_run_files(self.vast_dir, self.base):
                if rel not in campaign_data["_run_files"]:
                    campaign_data["_run_files"].append(rel)
            for rel in _collect_analysis_input_files(self.base, self.vast_dir):
                if (rel not in campaign_data["_input_files"]
                        and rel not in campaign_data["_run_files"]):
                    campaign_data["_input_files"].append(rel)
        finally:
            try:
                os.remove(temp_vast)
            except OSError:
                pass

        name_by_id = self._resolve_names(campaign_data, id_by_block)

        logger.debug("Composed %d param set(s) into %d config(s)",
                     len(param_sets), len(campaign_data.get("configs", [])))
        return campaign_data, name_by_id

    @staticmethod
    def _resolve_names(campaign_data: dict, id_by_block: dict) -> dict:
        """Map each ``ParamSet.id`` to its single produced config name, enforcing
        the search 1:1 contract.

        A variation in ``search.variations`` renames its output (``c<id>-1``) and
        may expand a block combinatorially (e.g. ``num_paths > 1`` or a
        list-valued ``path_length``) while ``_config_name`` stays the parent
        block name. Search looks up results by the produced config name, so each
        block must yield exactly one config; an expansion (or an empty result) is
        a configuration error — fail early and clearly.
        """
        produced: dict[str, list] = defaultdict(list)
        for c in campaign_data.get("configs", []):
            produced[c.get("_config_name")].append(c.get("name"))
        name_by_id = {}
        for block_name, ps_id in id_by_block.items():
            got = produced.get(block_name, [])
            if len(got) == 1:
                name_by_id[ps_id] = got[0]
                continue
            if not got:
                # A variation filtered everything out for this one param set (e.g. an
                # impossible path/obstacle constraint) -- omit it from name_by_id rather
                # than failing the whole batch; the caller records it as a failed
                # evaluation and moves on with the rest of the group.
                logger.warning(
                    "Search variation produced no config for param set '%s' -- "
                    "treating it as a failed evaluation.", block_name)
                continue
            raise ValueError(
                f"Search variation expanded param set '{block_name}' into "
                f"{len(got)} configs ({got}). Each search param set must map to "
                f"exactly one config. Make every expanding parameter scalar: "
                f"PathVariationRandom num_paths=1 and scalar path_length/"
                f"num_goal_poses_per_m; ObstacleVariation count=1 and one amount/"
                f"max_distance per obstacle_configs entry; FloorplanVariation "
                f"num_variations=1.")
        return name_by_id


def preview_search_sample(vast_file: str, sample_size: int = 0) -> dict:
    """Compose a representative sample of a ``search:``-mode ``.vast``'s configs.

    A search ``.vast`` has no top-level ``configuration:`` block — its variations
    live under ``search.variations`` and are expanded only per sampled
    :class:`ParamSet`, at run time. ``generate_scenario_variations`` alone
    therefore reports zero configs for one, which reads as "empty/broken
    campaign" rather than "not expandable that way". This runs the *same*
    pipeline a real batch uses (``build_strategy(...).ask(n)`` ->
    :meth:`Compose.compose`) against a small sample, so a pre-flight check sees
    what the campaign would actually produce — including which draws are
    infeasible, which is the whole point of checking before spending compute.

    Returns ``{sampled, distinct, composed, infeasible, configs, runs_per_config}``, where
    ``sampled`` is how many parameter sets the strategy DREW and ``distinct`` how many
    cells those were -- they differ whenever a draw repeats, which a discrete space does
    routinely. ``infeasible`` lists ``{name, params}`` per distinct param set that could
    not be composed, and ``configs`` are the composed config dicts.
    """
    from robovast.common.config import validate_config
    from robovast.search.strategy import build_strategy

    vast_file = os.path.abspath(vast_file)
    search_cfg = validate_config(load_config(vast_file)).search
    if search_cfg is None:
        raise ValueError("preview_search_sample called without a 'search' block")

    # The campaign's own batch size is the representative sample — it is what one
    # real ask/tell round draws — capped, because composing runs the variation
    # plugins for real and a preview should stay cheap.
    n = sample_size or min(search_cfg.per_batch, _PREVIEW_SAMPLE_CAP)
    # Collapsed exactly as a real batch is, so the preview reports what the campaign would
    # compose rather than failing on a repeat the campaign itself would have absorbed. Both
    # counts are reported: on a discrete space they differ by a lot, and a preview that
    # showed only the survivors would look like a strategy proposing fewer sets than the
    # campaign asked for.
    drawn = build_strategy(search_cfg, os.path.dirname(vast_file)).ask(n)
    param_sets = distinct_draws(drawn, "This preview")

    with tempfile.TemporaryDirectory(prefix="robovast_preview_") as artifacts:
        campaign_data, name_by_id = Compose(vast_file).compose(param_sets, artifacts)

    return {
        "sampled": len(drawn),
        "distinct": len(param_sets),
        "composed": len(name_by_id),
        "infeasible": [{"name": config_name_for(ps), "params": ps.values}
                       for ps in param_sets if ps.id not in name_by_id],
        "configs": campaign_data.get("configs", []),
        "runs_per_config": campaign_data.get("execution", {}).get("runs", 1),
    }
