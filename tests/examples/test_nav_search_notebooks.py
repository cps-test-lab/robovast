# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The nav_search campaign notebooks execute, against data shaped like their own campaign.

**Why this needs a test at all.** Each notebook guards its plotting with ``if not
scored.empty``, which is right -- a campaign stopped early has nothing to draw. The cost is
that a wrong column name, a renamed measure, or a search space that gained a factor produces
a blank cell and no error, and the failure is invisible until someone looks at a rendered
notebook and sees nothing. That is the worst possible moment: after the campaign is spent.

So the fixture is built from **each ``.vast``'s own ``search_space``**, and the notebook is
executed. A first version of this check invented parameter names and three notebooks raised
``KeyError`` -- the fixture's fault, not theirs, and the reason the columns are now derived
from the file the campaign will actually run rather than written out here a second time.

Two units are recorded as ``no_sample`` on purpose. A search records a cell it could not
score (see ``NavExtract`` raising ``NoSampleError`` when a config produced no ``test.xml``),
so surviving them is part of being correct rather than an edge case.
"""

import json
import pathlib
import random
import sqlite3

import pytest

nbformat = pytest.importorskip("nbformat")
NotebookClient = pytest.importorskip("nbclient").NotebookClient
pytest.importorskip("matplotlib")
pytest.importorskip("pandas")
yaml = pytest.importorskip("yaml")

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "configs" / "examples" / "nav_search"
MODES = ["none", "collision", "timeout", "goal_miss"]

#: The schema the notebooks query, verbatim from ``robovast.common.store`` -- only the two
#: tables they read. Written out rather than imported because the point is to catch a
#: notebook that drifts from the schema, and building the fixture through the writer would
#: hide exactly that drift.
SCHEMA = """
CREATE TABLE batch (id INTEGER PRIMARY KEY, campaign_id INTEGER, idx INTEGER, dir TEXT,
                    created_at REAL);
CREATE TABLE unit (id INTEGER PRIMARY KEY, batch_id INTEGER, paramset_id TEXT,
                   config_name TEXT, params_json TEXT, objective REAL,
                   objectives_json TEXT, measures_json TEXT, n_samples INTEGER,
                   status TEXT, result_dir TEXT, created_at REAL);
"""


def _notebooks():
    if not EXAMPLE.is_dir():
        return []
    return sorted(v for v in EXAMPLE.glob("nav_search_*.vast")
                  if (EXAMPLE / "analysis" / f"{v.stem}.ipynb").exists())


def _sample(spec, rnd):
    """One draw per declared factor, honouring its type."""
    if spec.get("type") == "categorical" or "values" in spec:
        return rnd.choice(list(spec.get("values") or ["a", "b"]))
    return round(rnd.uniform(float(spec.get("low", 0)), float(spec.get("high", 1))), 4)


def _campaign_db(path, space):
    """A search's ``campaign.db`` with 20 cells across 4 batches, 18 of them scored."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    rnd = random.Random(7)
    uid = 0
    for batch in range(4):
        conn.execute("INSERT INTO batch (id, campaign_id, idx) VALUES (?,1,?)",
                     (batch + 1, batch))
        for _ in range(5):
            uid += 1
            params = {name: _sample(spec, rnd) for name, spec in space.items()}
            robustness = round(rnd.uniform(-1.0, 0.6), 4)
            mode = "none" if robustness > 0 else rnd.choice(MODES[1:])
            conn.execute(
                "INSERT INTO unit (id, batch_id, paramset_id, config_name, params_json,"
                " objective, objectives_json, measures_json, n_samples, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, batch + 1, f"ps{uid}", f"cfg-{uid}", json.dumps(params), robustness,
                 json.dumps({"robustness": robustness,
                             "failure_rate": 0.0 if robustness > 0 else 1.0,
                             "time_to_goal": round(rnd.uniform(20, 119), 1)}),
                 json.dumps({"min_clearance": round(robustness * 0.05, 4),
                             "time_to_goal": round(rnd.uniform(20, 119), 1),
                             "recovery_count": rnd.randint(0, 4),
                             "failure_mode": mode}),
                 3, "no_sample" if uid in (7, 18) else "evaluated"))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("vast", _notebooks(), ids=lambda p: p.stem)
def test_the_notebook_executes_and_draws_something(vast, tmp_path):
    space = (yaml.safe_load(vast.read_text()).get("search") or {}).get("search_space") or {}
    assert space, f"{vast.name} declares no search_space; the fixture would have no columns"
    _campaign_db(tmp_path / "campaign.db", space)

    nb = nbformat.read(EXAMPLE / "analysis" / f"{vast.stem}.ipynb", as_version=4)
    for cell in nb.cells:
        if cell.cell_type == "code":
            # Exactly the substitution the service performs for the node being viewed.
            cell.source = cell.source.replace("DATA_DIR = ''", f"DATA_DIR = {str(tmp_path)!r}")
    NotebookClient(nb, timeout=180, kernel_name="python3").execute()

    figures = sum(1 for c in nb.cells for o in c.get("outputs", [])
                  if "image/png" in (o.get("data") or {}))
    assert figures, (f"{vast.stem} executed but drew nothing -- its plotting cell is guarded "
                     f"on a column the campaign does not produce")
    printed = "".join("".join(o.get("text", "")) for c in nb.cells
                      for o in c.get("outputs", []) if o.get("output_type") == "stream")
    # The two unscorable cells must be excluded from the scored population, not counted in it.
    assert "18 scored" in printed, f"{vast.stem} miscounted the scorable cells: {printed[:200]}"


def test_every_search_vast_has_a_notebook():
    """The generator writes one per ``.vast``; a new campaign without one is an omission,
    and this is where it is noticed rather than when someone goes looking for the analysis."""
    vasts = {v.stem for v in EXAMPLE.glob("nav_search_*.vast")}
    books = {n.stem for n in (EXAMPLE / "analysis").glob("nav_search_*.ipynb")}
    assert vasts and vasts == books, f"missing notebooks: {sorted(vasts - books)}"
