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

"""A search's stages: what each searches, what it stands on, and what it leaves.

A search is a line of stages run in the file's order (``search.stages``); a file without them
is one stage, named ``search``, over the whole space. Three rules make the line:

* **The space is stated once.** Every stage searches dimensions ``search_space`` declares, with
  their declared domains -- or, ``within`` an earlier stage, with that stage's region.
* **A stage has one answer: one cell**, selected from its own cells by its ``answer`` rule.
* **The search carries one point.** A dimension a stage does not search stands at the answer of
  the last stage that searched it, and before any did, at its ``initial``.

This module is pure: it turns the validated file into :class:`Stage` objects, builds the
``SearchConfig`` a stage's strategy is built from, and applies a stage's ``answer`` and
``region`` to its record through :mod:`robovast.search.chain`, the one implementation of
selecting and narrowing. The loop that runs the stages is the controller's.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from robovast.common.config import AnswerRule, ObjectiveSpec, RegionRule, SearchConfig

from . import chain

#: The name of the one stage a search without ``stages:`` runs.
IMPLICIT_STAGE = "search"

#: What ``answer`` and ``region`` take when not given.
DEFAULT_SELECT = "worst"
DEFAULT_PAD = 0.1
DEFAULT_MIN_CELLS = 3


class StageCondition(Exception):
    """A stage's ``answer`` or ``region`` found nothing to stand on; the campaign ends there.

    ``rule`` names which (``answer`` or ``region``); ``record`` is what the stage's record
    keeps of it: the rule as applied and what it saw.
    """

    def __init__(self, sentence: str, rule: str, record: dict):
        super().__init__(sentence)
        self.rule = rule
        self.record = record


@dataclass(frozen=True)
class Stage:
    """One stage of a search, as the file states it."""
    idx: int
    name: str
    over: tuple
    config: SearchConfig       # over the declared domains; see :meth:`config_over`
    within: Optional[str] = None
    answer: Optional[AnswerRule] = None
    region: Optional[RegionRule] = None
    #: The stage as written, for its definition digest.
    definition: Optional[dict] = None
    #: The one stage of a search written without ``stages:``. It is the whole search rather than a
    #: stage of a line, so a restart re-enters it as it re-enters any search.
    implicit: bool = False

    @property
    def objective(self) -> Optional[ObjectiveSpec]:
        """The stage's sole objective, ``None`` when it has several."""
        objectives = self.config.objectives
        return objectives[0] if len(objectives) == 1 else None

    def level(self, rule) -> Optional[float]:
        """The level *rule* reads: its own, else the stage's ``strategy_parameters.level``."""
        if rule is not None and rule.level is not None:
            return rule.level
        level = (self.config.strategy_parameters or {}).get("level")
        return None if level is None else float(level)

    def config_over(self, space: dict) -> SearchConfig:
        """The stage's config with its dimensions searched over *space* (``{dim: domain}``)."""
        if space == _dumped(self.config.search_space):
            return self.config
        return SearchConfig(**{**_base(self.config), "search_space": space})

    def described(self) -> dict:
        """What the store declares of the stage before it runs."""
        objective = self.objective
        return {"name": self.name, "over": list(self.over), "strategy": self.config.strategy,
                "objective": objective.name if objective else None,
                "direction": objective.direction if objective else None}

    def digest(self, space: dict, point: dict) -> str:
        """A hash of the stage's definition, the space it searches and the point it stands on."""
        blob = json.dumps({"stage": self.definition, "space": space, "point": point},
                          sort_keys=True, default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]  # nosec - not security


def _dumped(space) -> dict:
    """``{dim: domain}`` as plain mappings, without the ``initial`` only the line reads."""
    return {name: dim.model_dump(exclude={"initial"}, exclude_none=True)
            for name, dim in space.items()}


def _criterion(entry) -> dict:
    """A validated budget or stopping criterion as the single-key mapping a file writes."""
    fields = entry.model_dump()
    return {fields.pop("type"): fields}


def _base(cfg: SearchConfig) -> dict:
    """The keys of *cfg* a stage config is rebuilt from, as plain data."""
    return {
        "strategy": cfg.strategy, "strategy_parameters": dict(cfg.strategy_parameters or {}),
        "extract": cfg.extract.model_dump(),
        "objectives": [o.model_dump() for o in cfg.objectives],
        "per_batch": cfg.per_batch, "seed": cfg.seed,
        "budget": [_criterion(c) for c in cfg.budget] if cfg.budget else None,
        "stopping": [_criterion(c) for c in cfg.stopping] if cfg.stopping else None,
        "repetitions": cfg.repetitions.model_dump() if cfg.repetitions else None,
        "postprocessing": cfg.postprocessing,
    }


def stages_of(cfg: SearchConfig) -> list[Stage]:
    """The stages of *cfg*, in the file's order; one, :data:`IMPLICIT_STAGE`, without ``stages:``."""
    if not cfg.stages:
        return [Stage(idx=0, name=IMPLICIT_STAGE, over=tuple(cfg.search_space), config=cfg,
                      implicit=True,
                      definition={"strategy": cfg.strategy,
                                  "strategy_parameters": cfg.strategy_parameters})]
    out = []
    for idx, st in enumerate(cfg.stages):
        base = {
            "strategy": st.strategy,
            "strategy_parameters": dict(st.strategy_parameters or {}),
            "extract": cfg.extract.model_dump(),
            "objectives": [o.model_dump() for o in (st.objectives or cfg.objectives)],
            "per_batch": st.per_batch or cfg.per_batch,
            "seed": cfg.seed,
            "budget": [_criterion(c) for c in st.budget] if st.budget else None,
            "stopping": [_criterion(c) for c in st.stopping] if st.stopping else None,
            "repetitions": (st.repetitions or cfg.repetitions).model_dump()
            if (st.repetitions or cfg.repetitions) else None,
            "postprocessing": cfg.postprocessing,
            "search_space": {d: v for d, v in _dumped(cfg.search_space).items() if d in st.over},
        }
        out.append(Stage(idx=idx, name=st.name, over=tuple(st.over),
                         config=SearchConfig(**base), within=st.within, answer=st.answer,
                         region=st.region, definition=st.model_dump()))
    return out


def initial_point(cfg: SearchConfig) -> dict:
    """``{dim: initial}`` for every dimension that declares one: the point before any stage."""
    return {name: dim.initial for name, dim in cfg.search_space.items()
            if dim.initial is not None}


def declared_space(cfg: SearchConfig, stage: Stage) -> dict:
    """The declared domains of *stage*'s dimensions, as plain mappings."""
    return {d: v for d, v in _dumped(cfg.search_space).items() if d in stage.over}


def evaluation_objectives(cfg: SearchConfig) -> list[ObjectiveSpec]:
    """Every objective a stage of *cfg* reads, the search's first, each name once.

    A cell is scored once and may be read by any stage that re-proposes it, so the extractor is
    asked for all of them.
    """
    seen, out = set(), []
    for spec in list(cfg.objectives) + [o for st in (cfg.stages or [])
                                        for o in (st.objectives or [])]:
        if spec.name not in seen:
            seen.add(spec.name)
            out.append(spec)
    return out


def answer_of(stage: Stage, record: chain.Record) -> dict:
    """The one cell *stage*'s ``answer`` selects from its *record*, as the stage records it.

    Raises :class:`StageCondition` when no cell satisfies the rule.
    """
    rule = stage.answer
    select = rule.select or DEFAULT_SELECT
    level = stage.level(rule) if select == "nearest-level" else None
    applied = {"select": select}
    if rule.feasible:
        applied["feasible"] = rule.feasible
    if level is not None:
        applied["level"] = level
    pool = chain.feasible_cells(record, rule.feasible)
    if not pool:
        what = f" with {rule.feasible!r} true" if rule.feasible else ""
        sentence = f"{record.label} has no cell{what} to select from"
        raise StageCondition(sentence, "answer", {"rule": applied, "cells": 0,
                                                   "failed": sentence})
    cell = chain.select_cell(record, select, level=level, feasible=rule.feasible)
    return {"rule": applied, "unit_id": cell.unit_id, "objective": cell.objective,
            "values": {d: cell.params[d] for d in stage.over}}


def region_of(stage: Stage, record: chain.Record, declared: dict) -> dict:
    """The region *stage* leaves: the box its cells near the level span over *declared*.

    Raises :class:`StageCondition` when fewer cells than ``min_cells`` lie in the band.
    """
    rule = stage.region
    applied = {"level": stage.level(rule), "band": rule.band,
               "pad": DEFAULT_PAD if rule.pad is None else rule.pad,
               "min_cells": DEFAULT_MIN_CELLS if rule.min_cells is None else rule.min_cells}
    cells = chain.band_cells(record, applied["level"], rule.band)
    if len(cells) < applied["min_cells"]:
        sentence = (f"{record.label} has {len(cells)} cell(s) within {rule.band:g} of the "
                    f"level {applied['level']} for {record.objective!r}, fewer than the "
                    f"{applied['min_cells']} its region needs")
        raise StageCondition(sentence, "region", {"rule": applied, "cells": len(cells),
                                                   "failed": sentence})
    box, cells = chain.region_box(record, declared, level=applied["level"], band=rule.band,
                                  pad=applied["pad"], min_cells=applied["min_cells"])
    return {"rule": applied, "cells": len(cells), "box": box}


def carried(point: dict, stage: Stage) -> dict:
    """The values *stage* stands on: the point, less the dimensions it searches."""
    return {d: v for d, v in point.items() if d not in stage.over}


def full_values(point: dict, proposal: dict) -> dict:
    """A cell's values over every dimension: the point, overridden by what was proposed."""
    return {**point, **proposal}


def stage_values(values: dict, stage: Stage) -> dict:
    """The part of a cell's values *stage* searched: what its strategy proposed."""
    return {d: values[d] for d in stage.over if d in values}


__all__ = ["IMPLICIT_STAGE", "Stage", "StageCondition", "stages_of", "initial_point",
           "declared_space", "evaluation_objectives", "answer_of", "region_of", "carried",
           "full_values", "stage_values"]
