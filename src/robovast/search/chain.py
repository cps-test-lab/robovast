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

"""Derive a chain's next stage from a finished stage's record.

A chain is a sequence of ordinary campaigns in which each stage's search space is induced by
the stage before it. This module is the operator: it reads a finished campaign's
``campaign.db``, applies a stated rule to its cells, and writes the next stage's ``.vast``
from a template. Two edits are possible, each per dimension:

* **narrow** (``space``): the dimension stays searched, its domain cut to the cells within a
  band of a level -- a float or an integer to their span, padded and clipped to the
  template's own domain; a choice or a boolean to the values those cells hold.
* **fix** (``fix``): the dimension is no longer searched; every ``$<dim>`` marker in the
  template is replaced by its value in one selected cell, and the dimension leaves the
  template's ``search_space`` if it was there. A batch template, with no search block, takes
  fixed values too: a stage that runs listed configurations on the selected cell. This is how a template adds dimensions of its
  own while the upstream ones are held at the selected cell.

The cell is chosen by a rule applied to the record, never by looking: ``worst`` (the extreme the
objective's direction seeks: the minimum of a minimised objective, the maximum of a
maximised one),
``nearest-level`` (the smallest ``|objective - level|``) or ``feasible-min`` (the lowest
objective); ``feasible`` restricts any rule to cells whose named measure is true.

Every derived file records where it came from in ``metadata.derived_from``, one entry per
derivation appended to the template's own, so a chain's provenance is in the files its stages
ran from. See ``docs/search.rst``, "Chaining campaigns".
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

SELECT_RULES = ("worst", "nearest-level", "feasible-min")
OPERATOR = "derive-stage"


class ChainError(ValueError):
    """A derivation that cannot be made as asked; the message says why."""


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
    """A finished campaign's cells, read from its ``campaign.db``."""
    path: Path
    campaign: str
    config: dict
    objective: str
    direction: str
    cells: list[Cell] = field(default_factory=list)

    @property
    def search(self) -> dict:
        return self.config.get("search") or {}

    @property
    def search_space(self) -> dict:
        return self.search.get("search_space") or {}

    def level(self) -> Optional[float]:
        """The boundary level the record's strategy traced, when it states one."""
        level = (self.search.get("strategy_parameters") or {}).get("level")
        return None if level is None else float(level)


def _store_path(path: Path) -> Path:
    path = Path(path)
    if path.is_file():
        return path
    if path.is_dir():
        direct = path / "campaign.db"
        if direct.is_file():
            return direct
        nested = sorted(path.glob("*/campaign.db"))
        if len(nested) == 1:
            return nested[0]
        if len(nested) > 1:
            raise ChainError(f"{path} holds several campaigns ({len(nested)} campaign.db "
                             "files); name the one to derive from")
    raise ChainError(f"no campaign.db at {path}: give the file, or the directory "
                     "`vast campaign download --raw --extract` wrote")


def load_record(path, objective: Optional[str] = None) -> Record:
    """Read a campaign's cells from its ``campaign.db``.

    A cell counts when its unit scored a finite value of ``objective`` (the record's first
    objective when not named); a unit that composed or measured nothing is not a cell.
    """
    db = _store_path(path)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT name, config_json FROM campaign ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise ChainError(f"{db} records no campaign")
        config = json.loads(row["config_json"] or "{}")
        objectives = (config.get("search") or {}).get("objectives") or []
        if not objectives:
            raise ChainError(f"{row['name']} is not a search campaign: its configuration "
                             "declares no search objectives")
        names = [o.get("name") for o in objectives]
        name = objective or names[0]
        if name not in names:
            raise ChainError(f"{row['name']} has no objective {name!r}; it has {names}")
        direction = next(o.get("direction", "minimize") for o in objectives
                         if o.get("name") == name)
        units = con.execute(
            "SELECT unit.id AS id, batch.idx AS batch, unit.params_json AS params, "
            "unit.objectives_json AS objectives, unit.measures_json AS measures "
            "FROM unit JOIN batch ON unit.batch_id = batch.id ORDER BY batch.idx, unit.id"
        ).fetchall()
    finally:
        con.close()
    cells = []
    for u in units:
        value = json.loads(u["objectives"] or "{}").get(name)
        if value is None:
            continue
        value = float(value)
        if not math.isfinite(value):
            continue
        cells.append(Cell(unit_id=int(u["id"]), batch=int(u["batch"]),
                          params=json.loads(u["params"] or "{}"), objective=value,
                          measures=json.loads(u["measures"] or "{}")))
    return Record(path=db, campaign=str(row["name"]), config=config, objective=name,
                  direction=direction, cells=cells)


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
        raise ChainError(f"{record.campaign}: {len(missing)} cell(s) carry no measure "
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
        raise ChainError(f"{record.campaign} has no cell{what} to select from")

    def order(cell: Cell):
        return (cell.batch, cell.unit_id)

    if rule == "worst":
        sign = 1.0 if record.direction == "minimize" else -1.0
        return min(pool, key=lambda c: (sign * c.objective, order(c)))
    if rule == "feasible-min":
        return min(pool, key=lambda c: (c.objective, order(c)))
    if level is None:
        raise ChainError("nearest-level needs a level: give --level, or derive from a "
                         "record whose strategy states one")
    return min(pool, key=lambda c: (abs(c.objective - level), c.objective, order(c)))


def band_cells(record: Record, level: float, band: float) -> list[Cell]:
    """The cells whose objective lies within ``band`` of ``level``, in the objective's unit."""
    if band < 0:
        raise ChainError(f"band must be >= 0, got {band}")
    return [c for c in record.cells if abs(c.objective - level) <= band]


def narrow(dim: dict, values: list, pad: float) -> dict:
    """``dim`` (a template's search dimension) cut to what ``values`` span.

    A float or an integer: their span, widened by ``pad`` of the template's range on each
    side (in log space for a log one) and clipped to it; an integer's edges rounded outward
    onto its step. A choice or a boolean: the values held, in the template's order, as a choice.
    """
    kind = dim.get("type")
    if kind in ("choice", "bool"):
        domain = list(dim["values"]) if kind == "choice" else [False, True]
        held = [v for v in domain if v in values]
        unknown = [v for v in values if v not in domain]
        if unknown:
            raise ChainError(f"values {unknown} lie outside the template's domain {domain}")
        return {"type": "choice", "values": held}
    if kind not in ("float", "int"):
        raise ChainError(f"cannot narrow a dimension of type {kind!r}")
    low, high = float(dim["low"]), float(dim["high"])
    numbers = [float(v) for v in values]
    # The pad is taken where the codec spaces the dimension: in log space for a log one.
    scale, unscale = (math.log, math.exp) if dim.get("log") else (float, float)
    margin = pad * (scale(high) - scale(low))
    lo = max(low, unscale(scale(min(numbers)) - margin))
    hi = min(high, unscale(scale(max(numbers)) + margin))
    out = {k: v for k, v in dim.items() if k not in ("low", "high")}
    if kind == "int":
        # Rounded outward onto the template's own lattice, so every value it could take in the
        # box stays reachable.
        step, base = int(dim.get("step") or 1), int(dim["low"])
        out["low"] = base + math.floor((lo - base) / step) * step
        out["high"] = min(int(dim["high"]), base + math.ceil((hi - base) / step) * step)
    else:
        out["low"], out["high"] = round(lo, 6), round(hi, 6)
    return out


def _replace_markers(node, values: dict, used: set):
    if isinstance(node, dict):
        return {k: _replace_markers(v, values, used) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_markers(v, values, used) for v in node]
    if isinstance(node, str) and node.startswith("$") and node[1:] in values:
        used.add(node[1:])
        return values[node[1:]]
    return node


def derive(record_path, template_path, out_path, *, space=(), fix=(), select=None,
           feasible=None, level=None, band=None, pad=None, min_cells=None,
           objective=None) -> dict:
    """Write the next stage's ``.vast`` and return the provenance entry it carries.

    ``space`` names the dimensions to narrow to the band box, ``fix`` those to fix at the
    selected cell. Every argument given is used or refused: a dimension the template does not
    search cannot be narrowed, one the template has no ``$<dim>`` marker for cannot be fixed,
    and an option of narrowing without ``space`` or of selection without ``fix`` is refused.
    ``None`` is "not given": ``select`` then is ``worst``, ``pad`` 0.1 and ``min_cells`` 3.
    """
    space, fix = list(space), list(fix)
    if not space and not fix:
        raise ChainError("nothing to derive: name at least one --space or --fix dimension")
    unused = [name for name, value, used in (
        ("--band", band, space), ("--pad", pad, space), ("--min-cells", min_cells, space),
        ("--select", select, fix), ("--feasible", feasible, fix),
        ("--level", level, space or (fix and select == "nearest-level")),
    ) if value is not None and not used]
    if unused:
        raise ChainError(f"{', '.join(unused)} would change nothing here: --band, --pad and "
                         "--min-cells shape --space, --select and --feasible pick the cell "
                         "--fix reads, and --level serves --space or --select nearest-level")
    select = "worst" if select is None else select
    pad = 0.1 if pad is None else pad
    min_cells = 3 if min_cells is None else min_cells
    both = sorted(set(space) & set(fix))
    if both:
        raise ChainError(f"{both} are both narrowed and fixed; a dimension is one or the other")
    if pad < 0:
        raise ChainError(f"pad must be >= 0, got {pad}")
    record = load_record(record_path, objective)
    text = Path(template_path).read_text()
    template = yaml.safe_load(text)
    # The template's own leading comment (what it is, which file plugins it loads) stays at
    # the head of what is derived from it.
    lead = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        if line != "# Derive again rather than edit.":
            lead.append(line)
    if not isinstance(template, dict):
        raise ChainError(f"{template_path} is not a .vast mapping")
    # A batch template (no search block) can take fixed values; only narrowing needs a search.
    search = template.get("search")
    if space and not isinstance(search, dict):
        raise ChainError(f"{template_path} has no search block, so it has no dimension to narrow")
    tspace = search.setdefault("search_space", {}) if isinstance(search, dict) else {}
    if level is None:
        level = record.level()

    for dim in space + fix:
        if record.cells and dim not in record.cells[0].params:
            raise ChainError(f"{record.campaign} did not search {dim!r}; its cells carry "
                             f"{sorted(record.cells[0].params)}")
    entry: dict[str, Any] = {"campaign": record.campaign, "operator": OPERATOR,
                             "objective": record.objective}

    if space:
        missing = [d for d in space if d not in tspace]
        if missing:
            raise ChainError(f"the template does not search {missing}, so it cannot narrow them")
        if level is None:
            raise ChainError("--space needs a level: give --level, or derive from a record "
                             "whose strategy states one")
        if band is None:
            raise ChainError("--space needs --band, the half-width around the level in the "
                             "objective's unit")
        cells = band_cells(record, level, band)
        if len(cells) < min_cells:
            raise ChainError(
                f"{record.campaign} has {len(cells)} cell(s) within {band} of the level "
                f"{level} for {record.objective!r}, fewer than the {min_cells} a box needs")
        for dim in space:
            tspace[dim] = narrow(tspace[dim], [c.params[dim] for c in cells], pad)
        entry.update({"space": space, "level": level, "band": band, "pad": pad,
                      "cells": len(cells)})

    if fix:
        cell = select_cell(record, select, level=level, feasible=feasible)
        values = {d: cell.params[d] for d in fix}
        used: set = set()
        template = _replace_markers(template, values, used)
        unused = [d for d in fix if d not in used]
        if unused:
            raise ChainError(f"the template has no ${unused[0]} marker; fixing "
                             f"{unused} would change nothing")
        if isinstance(template.get("search"), dict):
            for d in fix:
                template["search"].get("search_space", {}).pop(d, None)
        entry.update({"fix": values, "select": select, "selected_unit": cell.unit_id,
                      "selected_objective": cell.objective})
        if select == "nearest-level":
            entry["level"] = level
        if feasible:
            entry["feasible"] = feasible

    meta = template.setdefault("metadata", {}) or {}
    template["metadata"] = meta
    meta["derived_from"] = list(meta.get("derived_from") or []) + [entry]
    header = (f"# Derived from campaign {record.campaign} by vast config {OPERATOR}; "
              "metadata.derived_from has the parameters.\n# Derive again rather than edit.\n")
    header += "".join(f"{line}\n" for line in lead)
    Path(out_path).write_text(header + yaml.safe_dump(template, sort_keys=False, width=100,
                                                      allow_unicode=True))
    return entry
