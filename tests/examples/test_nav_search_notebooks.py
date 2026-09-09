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


def _declared_notebook(vast):
    """The campaign-scope notebook a ``.vast`` declares, or None.

    Resolved from the file rather than guessed from the stem, because a notebook is declared
    by path and two campaigns may legitimately share one -- the six-factor pair does, since
    the view depends on the search space and not on which sampler explored it. Matching stems
    would forbid that, and would also miss a declared path that points at nothing.
    """
    doc = yaml.safe_load(vast.read_text()) or {}
    books = (((doc.get("visualization") or {}).get("results") or {})
             .get("explorer") or {}).get("notebooks") or []
    for entry in books:
        for scopes in (entry or {}).values():
            path = (scopes or {}).get("campaign")
            if path:
                return EXAMPLE / path
    return None


def _search_vasts():
    """Every campaign in the example that runs a SEARCH; the batch grid has no search_space."""
    if not EXAMPLE.is_dir():
        return []
    return sorted(v for v in EXAMPLE.glob("*.vast")
                  if (yaml.safe_load(v.read_text()) or {}).get("search"))


def _notebooks():
    return [v for v in _search_vasts()
            if (_declared_notebook(v) or pathlib.Path("/nonexistent")).exists()]


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
                 # Only the OPTIMISED objective goes here. `failure_rate` and
                 # `time_to_goal` are measures: the framework refuses an extractor that
                 # reports one name in both places, and reporting unoptimised values
                 # beside the objective made a single-objective search look
                 # multi-objective and left the scalar `unit.objective` NULL. The fixture
                 # has to model the contract the extractor actually obeys, or the notebook
                 # is checked against data no campaign can produce.
                 json.dumps({"robustness": robustness}),
                 json.dumps({"min_clearance": round(robustness * 0.05, 4),
                             "failure_rate": 0.0 if robustness > 0 else 1.0,
                             "time_to_goal": round(rnd.uniform(20, 119), 1),
                             "recovery_count": rnd.randint(0, 4),
                             "failure_mode": mode}),
                 3, "no_sample" if uid in (7, 18) else "evaluated"))
    conn.commit()
    conn.close()


#: What each notebook must be seen to compute, keyed by the campaign it belongs to. These are
#: the questions the README gives each campaign, so a change that makes two notebooks print
#: the same thing fails here rather than being noticed when someone reads the analysis.
ANSWERS = {
    "nav_search_random": ["the fraction of the space that fails", "Wilson interval"],
    "nav_search_halton": ["the fraction of the space that fails", "Wilson interval"],
    "nav_search_tpe": ["how FEW evaluations found it", "best crossing found"],
    "nav_search_cmaes": ["how FEW evaluations found it", "best crossing found"],
    "nav_search_qd": ["how many KINDS of trouble exist", "distinct failure modes found"],
    "nav_search_boundary": ["WHERE it starts failing", "cells within +-0.05 of the boundary"],
    "nav_search_adaptive_reps": ["what one budget buys", "repetitions per cell"],
    "nav_search_minimax": ["which TUNING survives the worst", "tunings evaluated"],
    # The six-factor pair shares one view, so both campaigns are checked against the same
    # phrases -- what differs between them is the sampler, not the question.
    "nav_search_random_6d": ["what six factors cost this sampler", "runs to reach"],
    "nav_search_tpe_6d": ["what six factors cost this sampler", "runs to reach"],
}


@pytest.mark.parametrize("vast", _notebooks(), ids=lambda p: p.stem)
def test_the_notebook_executes_and_draws_something(vast, tmp_path):
    space = (yaml.safe_load(vast.read_text()).get("search") or {}).get("search_space") or {}
    assert space, f"{vast.name} declares no search_space; the fixture would have no columns"
    _campaign_db(tmp_path / "campaign.db", space)

    nb = nbformat.read(_declared_notebook(vast), as_version=4)
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
    # The campaign's OWN answer must actually compute. Every answer block is guarded on a
    # column it needs -- `m_failure_mode` for the archive, the outer parameters for minimax --
    # so a renamed column does not raise: it prints nothing, and the campaign's central claim
    # quietly goes missing from its own analysis. Asserted per notebook rather than against
    # one shared string, because each campaign is run to answer a different question and a
    # notebook printing its neighbour's numbers is exactly the failure worth catching.
    assert "What this campaign is FOR" in printed, (
        f"{vast.stem} printed no answer block at all")
    for phrase in ANSWERS[vast.stem]:
        assert phrase in printed, (
            f"{vast.stem} did not print {phrase!r} -- its answer is guarded on a column "
            f"the extractor no longer produces under that name")
    assert "nan" not in printed.lower(), (
        f"{vast.stem} printed part of its answer as nan -- it is reading a column the "
        f"extractor no longer produces under that name")


def test_every_campaign_declares_a_notebook_that_exists():
    """A campaign without a notebook has no analysis, and this is where that is noticed
    rather than when someone goes looking for it after the campaign is spent. Each notebook
    is a standalone artifact -- there is no generator to regenerate a missing one from.

    Checked against what each file DECLARES, over every campaign in the example rather than
    the ``nav_search_*`` ones alone. Matching a notebook to a ``.vast`` by stem tested a
    convention instead of the invariant: it missed the batch grid entirely, whose name does
    not carry the prefix and which for a long time had no view at all, and it could not tell
    a declared path pointing at nothing from a missing file."""
    vasts = sorted(EXAMPLE.glob("*.vast"))
    assert vasts, "no campaigns found in the example"
    missing = [v.name for v in vasts if _declared_notebook(v) is None]
    assert not missing, f"campaigns declaring no campaign-scope notebook: {missing}"
    dangling = [f"{v.name} -> {_declared_notebook(v)}"
                for v in vasts if not _declared_notebook(v).exists()]
    assert not dangling, f"declared notebooks that do not exist: {dangling}"
    # ANSWERS covers the SEARCH campaigns, which are the ones executed above; the batch grid
    # has no search_space for the fixture to build from and is checked for its view only.
    searched = {v.stem for v in _search_vasts()}
    assert searched == set(ANSWERS), (
        f"ANSWERS is out of step with the campaigns: {sorted(searched ^ set(ANSWERS))}")
