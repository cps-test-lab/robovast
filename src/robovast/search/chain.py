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

"""The rules a search in stages applies between two stages: select a cell, narrow a region.

Between two stages the controller reads the stage that just ended from the campaign's own
``campaign.db`` (:func:`load_record`) and applies the stage's rules to its cells:

* **select** (``answer``): the one cell a rule picks -- ``worst`` (the extreme the objective's
  direction seeks), ``nearest-level`` (the smallest ``|objective - level|``) or ``feasible-min``
  (the lowest objective) -- among the cells whose ``feasible`` measure is true
  (:func:`select_cell`, :func:`feasible_cells`);
* **narrow** (``region``): the cells whose objective lies within ``band`` of ``level``, and the
  box they span over the stage's dimensions -- a float or an integer their span, widened by
  ``pad`` of the declared range and clipped to it; a choice or a boolean the values those cells
  hold -- refused below ``min_cells`` (:func:`band_cells`, :func:`narrow`, :func:`region_box`).

The cell is chosen by a rule applied to the record, never by looking, so the same record always
gives the same cell. An internal module with no command: see ``docs/search.rst``, "A search in
stages".
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import TypeAdapter, ValidationError

from robovast.common.config import SELECT_RULES, SearchDim


class ChainError(ValueError):
    """A rule that cannot be applied as asked; the message says why."""


@dataclass(frozen=True)
class Cell:
    """One evaluated cell of a record: what was proposed and what it scored."""
    unit_id: int
    batch: int
    params: dict
    objective: float
    measures: dict


@dataclass
class Record:
    """A finished campaign's cells, read from its ``campaign.db``: all of them, or one stage's."""
    path: Path
    campaign: str
    config: dict
    objective: str
    direction: str
    cells: list[Cell] = field(default_factory=list)
    #: The stage the cells are of, ``None`` for a record read whole.
    stage: Optional[str] = None

    @property
    def label(self) -> str:
        """How a sentence names what the cells are of: the stage, or the campaign."""
        return f"stage {self.stage!r}" if self.stage else self.campaign

    @property
    def search(self) -> dict:
        return self.config.get("search") or {}

    @property
    def search_space(self) -> dict:
        return self.search.get("search_space") or {}

    def _stage_config(self) -> dict:
        return next((st for st in self.search.get("stages") or []
                     if st.get("name") == self.stage), {})

    def level(self) -> Optional[float]:
        """The boundary level the strategy that produced the cells traced, when it states one."""
        source = self._stage_config() if self.stage else self.search
        level = (source.get("strategy_parameters") or {}).get("level")
        return None if level is None else float(level)


def _store_path(path: Path) -> Path:
    """The ``campaign.db`` at *path*: the file itself, or the one in the directory named."""
    path = Path(path)
    if path.is_file():
        return path
    if (path / "campaign.db").is_file():
        return path / "campaign.db"
    raise ChainError(f"no campaign.db at {path}")


def load_record(path, objective: Optional[str] = None, stage: Optional[str] = None) -> Record:
    """Read a campaign's cells from its ``campaign.db``: one stage's, or every cell.

    A record of several stages needs ``stage``; a record of one, or one recorded before
    stages, is read whole without it. A cell counts when its unit scored a finite value of
    ``objective`` -- the stage's sole objective, else the record's first, when not named; a
    unit that composed or measured nothing is not a cell. A cell a stage re-proposed after an
    earlier batch measured it is one of that stage's cells, read from the unit that measured it.
    """
    db = _store_path(path)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT id, name, config_json FROM campaign ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise ChainError(f"{db} records no campaign")
        config = json.loads(row["config_json"] or "{}")
        search = config.get("search") or {}
        stages = _stages_of(con, row["id"])
        stage_id = None
        if stage is not None:
            if stage not in stages:
                raise ChainError(f"{row['name']} has no stage {stage!r}; it has "
                                 f"{list(stages) or 'none'}")
            stage_id = stages[stage]
        elif len(stages) > 1:
            raise ChainError(f"{row['name']} has stages {list(stages)}; name the one to read")
        objectives = search.get("objectives") or []
        stage_cfg = next((st for st in search.get("stages") or [] if st.get("name") == stage),
                         {})
        objectives = stage_cfg.get("objectives") or objectives
        if not objectives:
            raise ChainError(f"{row['name']} is not a search campaign: its configuration "
                             "declares no search objectives")
        names = [o.get("name") for o in objectives]
        name = objective or names[0]
        if name not in names:
            raise ChainError(f"{row['name']} has no objective {name!r}; it has {names}")
        direction = next(o.get("direction", "maximize") for o in objectives
                         if o.get("name") == name)
        where = "" if stage_id is None else "WHERE batch.stage_id = ?"
        units = con.execute(
            "SELECT COALESCE(m.id, unit.id) AS id, batch.idx AS batch, "
            "COALESCE(m.params_json, unit.params_json) AS params, "
            "COALESCE(m.objectives_json, unit.objectives_json) AS objectives, "
            "COALESCE(m.measures_json, unit.measures_json) AS measures "
            "FROM unit JOIN batch ON unit.batch_id = batch.id "
            "LEFT JOIN unit m ON unit.status = 'recalled' AND m.id = unit.recalled_from "
            f"{where} ORDER BY batch.idx, unit.id",
            () if stage_id is None else (stage_id,)).fetchall()
    finally:
        con.close()
    cells, seen = [], set()
    for u in units:
        value = json.loads(u["objectives"] or "{}").get(name)
        if value is None or u["id"] in seen:
            continue
        value = float(value)
        if not math.isfinite(value):
            continue
        seen.add(u["id"])
        cells.append(Cell(unit_id=int(u["id"]), batch=int(u["batch"]),
                          params=json.loads(u["params"] or "{}"), objective=value,
                          measures=json.loads(u["measures"] or "{}")))
    return Record(path=db, campaign=str(row["name"]), config=config, objective=name,
                  direction=direction, cells=cells, stage=stage)


def _stages_of(con, campaign_row_id) -> dict:
    """``{name: id}`` of a campaign's stages, in order; empty for a store without them."""
    try:
        return {r["name"]: r["id"] for r in con.execute(
            "SELECT id, name FROM stage WHERE campaign_id = ? ORDER BY idx", (campaign_row_id,))}
    except sqlite3.OperationalError:     # a store from before stages were recorded
        return {}


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    try:
        return bool(value) and not (isinstance(value, float) and math.isnan(value))
    except (TypeError, ValueError):
        return False


def feasible_cells(record: Record, measure: Optional[str]) -> list[Cell]:
    """The record's cells, restricted to those whose ``measure`` is true when one is named."""
    if measure is None:
        return list(record.cells)
    missing = [c.unit_id for c in record.cells if measure not in c.measures]
    if missing:
        raise ChainError(f"{record.label}: {len(missing)} cell(s) carry no measure "
                         f"{measure!r}, so their feasibility is unknown")
    return [c for c in record.cells if _truthy(c.measures[measure])]


def select_cell(record: Record, rule: str, *, level: Optional[float] = None,
                feasible: Optional[str] = None) -> Cell:
    """The one cell ``rule`` picks from the record.

    Ties go to the lower objective, then to the earlier cell (batch, then unit), so the same
    record always yields the same cell.
    """
    if rule not in SELECT_RULES:
        raise ChainError(f"unknown selection rule {rule!r}; one of {', '.join(SELECT_RULES)}")
    pool = feasible_cells(record, feasible)
    if not pool:
        what = f" with {feasible!r} true" if feasible else ""
        raise ChainError(f"{record.label} has no cell{what} to select from")

    def order(cell: Cell):
        return (cell.batch, cell.unit_id)

    if rule == "worst":
        sign = 1.0 if record.direction == "minimize" else -1.0
        return min(pool, key=lambda c: (sign * c.objective, order(c)))
    if rule == "feasible-min":
        return min(pool, key=lambda c: (c.objective, order(c)))
    if level is None:
        raise ChainError("nearest-level needs a level: give the rule one, or read a stage "
                         "whose strategy states one")
    return min(pool, key=lambda c: (abs(c.objective - level), c.objective, order(c)))


def band_cells(record: Record, level: float, band: float) -> list[Cell]:
    """The cells whose objective lies within ``band`` of ``level``, in the objective's unit."""
    if band < 0:
        raise ChainError(f"band must be >= 0, got {band}")
    return [c for c in record.cells if abs(c.objective - level) <= band]


def region_box(record: Record, dims: dict, *, level: float, band: float, pad: float,
               min_cells: int) -> tuple[dict, list[Cell]]:
    """The box the cells within ``band`` of ``level`` span over *dims*, and those cells.

    *dims* maps each dimension to narrow to its domain as declared; each becomes what
    :func:`narrow` makes of the cells' values. Fewer than ``min_cells`` cells in the band is
    refused rather than widened.
    """
    cells = band_cells(record, level, band)
    if len(cells) < min_cells:
        raise ChainError(
            f"{record.label} has {len(cells)} cell(s) within {band} of the level "
            f"{level} for {record.objective!r}, fewer than the {min_cells} a box needs")
    box = {}
    for dim, domain in dims.items():
        try:
            box[dim] = narrow(domain, [c.params[dim] for c in cells], pad)
        except ChainError as e:
            raise ChainError(f"{dim!r}: {e}") from e
    return box, cells


_DIM = TypeAdapter(SearchDim)
_DIGITS = 6


def _checked(dim: dict, what: str) -> dict:
    """``dim`` as given, once the configuration's own schema accepts it."""
    try:
        _DIM.validate_python(dim)
    except ValidationError as e:
        raise ChainError(f"{what} is no search dimension: {e.errors()[0]['msg']}") from e
    return dim


def _outward(value: float, down: bool) -> float:
    """``value`` at ``_DIGITS`` significant digits, rounded away from the box's inside, so an
    edge the record keeps never cuts off the cell it was taken from."""
    if value == 0:
        return 0.0
    decimals = _DIGITS - 1 - math.floor(math.log10(abs(value)))
    quantum = 10.0 ** -decimals
    rounded = round(value, decimals)
    slack = abs(value) * 1e-12      # float noise is not a reason to step outward
    if down and rounded > value + slack:
        rounded -= quantum
    elif not down and rounded < value - slack:
        rounded += quantum
    return round(rounded, max(decimals, 0))


def narrow(dim: dict, values: list, pad: float) -> dict:
    """``dim`` (a declared search dimension) cut to what ``values`` span.

    A float or an integer: their span, widened by ``pad`` of the declared range on each
    side (in log space for a log one) and clipped to it; a float's edges rounded outward at
    six significant digits, an integer's outward onto its step. A choice or a boolean: the
    values held, in the declared order, as a choice. Values that lie wholly outside the
    declared range are refused, as a value outside a choice's is.
    """
    kind = dim.get("type")
    _checked(dim, f"the declared dimension {dim}")
    if kind in ("choice", "bool"):
        domain = list(dim["values"]) if kind == "choice" else [False, True]
        held = [v for v in domain if v in values]
        unknown = [v for v in values if v not in domain]
        if unknown:
            raise ChainError(f"values {unknown} lie outside the declared domain {domain}")
        return {"type": "choice", "values": held}
    if kind not in ("float", "int"):
        raise ChainError(f"cannot narrow a dimension of type {kind!r}")
    low, high = float(dim["low"]), float(dim["high"])
    numbers = [float(v) for v in values]
    if min(numbers) > high or max(numbers) < low:
        raise ChainError(f"values {min(numbers)} to {max(numbers)} lie outside the declared "
                         f"range {dim['low']} to {dim['high']}")
    # The pad is taken where the codec spaces the dimension: in log space for a log one.
    scale, unscale = (math.log, math.exp) if dim.get("log") else (float, float)
    margin = pad * (scale(high) - scale(low))
    lo = max(low, unscale(scale(min(numbers)) - margin))
    hi = min(high, unscale(scale(max(numbers)) + margin))
    out = {k: v for k, v in dim.items() if k not in ("low", "high")}
    if kind == "int":
        # Rounded outward onto the dimension's own lattice, so every value it could take in the
        # box stays reachable.
        step, base = int(dim.get("step") or 1), int(dim["low"])
        out["low"] = base + math.floor((lo - base) / step) * step
        out["high"] = min(int(dim["high"]), base + math.ceil((hi - base) / step) * step)
    else:
        out["low"] = max(low, _outward(lo, down=True))
        out["high"] = min(high, _outward(hi, down=False))
    return _checked(out, f"the narrowed dimension {out}")
