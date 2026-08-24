# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""CampaignController orchestration (fake backend) + CampaignStore.

Covers both modes: a strategy-driven *search* campaign and a strategy-less
*batch* campaign, plus the live store records (one batch per ask/tell round /
one batch for batch mode).
"""

# Tests exercise store internals and import schema helpers lazily.
# pylint: disable=import-outside-toplevel,protected-access

import json
import os
import sqlite3

import pytest

from robovast.common.config import SearchConfig
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.controller import CampaignController
from robovast.search.evaluator import Evaluator
from robovast.search.strategy import SearchStrategy, build_strategy
from robovast.search.types import ParamSet, SearchReport


def _cfg(batches=2, per_batch=3, stopping=None):
    budget = [{"batches": batches}]
    return SearchConfig(
        strategy="random",
        search_space={"x": {"type": "float", "low": 0, "high": 1}},
        extract={"plugin": "failure_rate"},
        objectives=[{"name": "failure_rate", "direction": "maximize"}],
        per_batch=per_batch, budget=budget, seed=1, stopping=stopping,
    )


def _write_test_xml(run_dir, failures):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "test.xml"), "w") as f:
        f.write(f'<testsuite errors="0" failures="{failures}" tests="1">'
                f'<testcase name="t" time="1.0"/></testsuite>')


class FakeBackend(ExecutionBackend):
    """Writes per-config test.xml under the campaign root (alternating pass/fail)."""

    def __init__(self):
        self.batch_runs = []  # reps requested per run_batch call

    def run_batch(self, campaign_data, *, campaign_root, batch_tag, runs, options,
                  whole_campaign=False):
        self.batch_runs.append(runs)
        for i, cfg in enumerate(campaign_data["configs"]):
            failures = i % 2  # alternate failing / passing configs
            for run in range(runs):
                _write_test_xml(os.path.join(campaign_root, cfg["name"], str(run)), failures)


class FakeCompose:
    def compose(self, param_sets, output_dir):
        name_by_id = {ps.id: f"c{ps.id}" for ps in param_sets}
        campaign_data = {"execution": {"containers": {"scenario": {"image": "img"}}, "runs": 1},
                         "configs": [{"name": n} for n in name_by_id.values()]}
        return campaign_data, name_by_id


class FakeComposePartialFailure:
    """Mimics Compose._resolve_names omitting one param set's id from name_by_id
    -- e.g. a variation plugin (ObstacleVariation, ...) failed to compose it."""

    def __init__(self, fail_index=0):
        self.fail_index = fail_index

    def compose(self, param_sets, output_dir):
        name_by_id, configs = {}, []
        for i, ps in enumerate(param_sets):
            if i == self.fail_index:
                continue
            name = f"c{ps.id}"
            name_by_id[ps.id] = name
            configs.append({"name": name})
        campaign_data = {"execution": {"containers": {"scenario": {"image": "img"}}, "runs": 1},
                         "configs": configs}
        return campaign_data, name_by_id


def _search_controller(cfg, tmp_path, strategy=None, runs=2, compose=None, evaluator=None):
    from robovast.search.stopping import build_stop_conditions
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    backend = FakeBackend()
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=runs, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), strategy=strategy or build_strategy(cfg),
        evaluator=evaluator or Evaluator(cfg, str(tmp_path)), compose=compose or FakeCompose(),
        per_batch=cfg.per_batch, stop_conditions=build_stop_conditions(cfg))
    return controller, store, backend


def test_search_runs_batches_and_records(tmp_path):
    cfg = _cfg(batches=2, per_batch=3)
    controller, store, backend = _search_controller(cfg, tmp_path)
    report = controller.run()

    assert backend.batch_runs == [2, 2]
    assert len(report.evaluations) == 6
    assert {next(iter(e.objectives.values())) for e in report.evaluations} <= {0.0, 1.0}
    assert report.best.objectives["failure_rate"] == 1.0
    assert all(e.n_samples == 2 for e in report.evaluations)

    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT mode FROM campaign").fetchone()[0] == "search"
    assert conn.execute("SELECT COUNT(*) FROM unit").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 2
    assert conn.execute("SELECT DISTINCT n_samples FROM unit").fetchall() == [(2,)]
    row = conn.execute("SELECT objectives_json, measures_json FROM unit LIMIT 1").fetchone()
    assert "failure_rate" in json.loads(row[0]) and json.loads(row[1]) == {}
    conn.close()
    store.close()

    # All config dirs are flat under the one campaign root (no per-batch nesting).
    campaign_root = tmp_path / "camp"
    config_dirs = [d for d in campaign_root.iterdir() if d.is_dir() and d.name.startswith("c")]
    assert len(config_dirs) == 6


class _Fixed(SearchStrategy):
    PARAMS_MODEL = None

    def __init__(self, cfg, param_sets):
        super().__init__(cfg, {})
        self._param_sets = param_sets
        self._done = False
        self.told = []

    def ask(self, n):
        return self._param_sets

    def tell(self, evaluations):
        self.told = evaluations
        self._done = True

    def report(self):
        return SearchReport(evaluations=self.told)


def test_stopping_target_objective_halts_early(tmp_path):
    # FakeBackend makes config #1 fail -> failure_rate 1.0 in batch 0, so a
    # target of 1.0 stops after the first batch despite the batches budget of 5.
    cfg = _cfg(batches=5, per_batch=3,
               stopping=[{"target_objective": 1.0}])
    controller, store, backend = _search_controller(cfg, tmp_path)
    report = controller.run()
    assert len(backend.batch_runs) == 1            # stopped after batch 0
    # Outcome persisted (parseable) on the campaign row + in the report.
    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 1
    row = conn.execute("SELECT stop_kind, batches FROM campaign").fetchone()
    assert row == ("target_objective", 1)
    conn.close()
    store.close()
    assert report.extra["stop"]["kind"] == "target_objective"


def test_no_stopping_runs_full_budget(tmp_path):
    cfg = _cfg(batches=3, per_batch=2)             # only the batches budget
    controller, store, backend = _search_controller(cfg, tmp_path)
    report = controller.run()
    assert len(backend.batch_runs) == 3            # full batches budget
    assert report.extra["stop"]["kind"] == "batches"
    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT stop_kind FROM campaign").fetchone()[0] == "batches"
    conn.close()
    store.close()


def test_n_reps_override_groups_runs(tmp_path):
    cfg = _cfg(batches=1, per_batch=3)
    param_sets = [ParamSet(values={"x": 0.1}, n_reps=5),
                  ParamSet(values={"x": 0.2}, n_reps=5),
                  ParamSet(values={"x": 0.3})]
    controller, store, backend = _search_controller(
        cfg, tmp_path, strategy=_Fixed(cfg, param_sets), runs=2)
    report = controller.run()
    assert sorted(backend.batch_runs) == [2, 5]
    by_x = {ev.params.values["x"]: ev.n_samples for ev in report.evaluations}
    assert by_x[0.1] == 5 and by_x[0.2] == 5 and by_x[0.3] == 2
    store.close()


def test_a_composition_failure_is_recorded_and_skipped_not_fatal(tmp_path):
    """One param set failing to compose (Compose._resolve_names omitting its id, e.g. a
    probabilistic ObstacleVariation placement failure) must not abort the batch: it is
    recorded as `composition_failed` and left out of what the strategy is told, while
    the rest of the batch is evaluated normally."""
    cfg = _cfg(batches=1, per_batch=3)
    param_sets = [ParamSet(values={"x": 0.1}),
                  ParamSet(values={"x": 0.2}),
                  ParamSet(values={"x": 0.3})]
    controller, store, _backend = _search_controller(
        cfg, tmp_path, strategy=_Fixed(cfg, param_sets),
        compose=FakeComposePartialFailure(fail_index=0))

    report = controller.run()  # must not raise

    assert len(report.evaluations) == 2  # the failed param set is excluded
    assert {ev.params.values["x"] for ev in report.evaluations} == {0.2, 0.3}

    conn = sqlite3.connect(store.db_path)
    rows = conn.execute(
        "SELECT paramset_id, config_name, status, n_samples, result_dir FROM unit"
    ).fetchall()
    conn.close()
    store.close()

    assert len(rows) == 3  # every param set is recorded, including the failed one
    failed = [r for r in rows if r[2] == "composition_failed"]
    assert len(failed) == 1
    assert failed[0][0] == param_sets[0].id
    assert failed[0][1] == "" and failed[0][3] == 0 and failed[0][4] == ""


def test_end_batch_progress_reports_resultless_runs(tmp_path):
    """When some runs produce no result, the batch-end progress reports the split.

    Previously it optimistically set completed == total, hiding partial-batch
    failures; now it reads the real produced-result count and reports the failed
    remainder so the monitor / MCP can surface it.
    """
    from robovast.execution.control_server import ControllerState

    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    backend = FakeBackend()
    # 3 of the 4 expected runs produced results
    backend.count_run_artifacts = lambda cid, root: 3
    state = ControllerState()
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=2, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), batch_campaign_data={"configs": []}, state=state)
    controller._poller = object()   # enable the progress path without a live thread
    controller._batch_baseline = 0
    controller._batch_total = 4
    controller._end_batch_progress()

    runs = state.snapshot().runs
    # The run that produced no artifact counts as no_result, not failed: `failed` is
    # reserved for a run that delivered a result whose verdict is a failure.
    assert (runs.completed, runs.total, runs.no_result, runs.failed) == (3, 4, 1, 0)
    store.close()


def test_batch_mode_records_one_batch(tmp_path):
    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    backend = FakeBackend()
    campaign_data = {"execution": {"containers": {"scenario": {"image": "img"}}, "runs": 2},
                     "configs": [{"name": "ca", "config": {"speed": 1.0}},
                                 {"name": "cb", "config": {"speed": 2.0}}]}
    controller = CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=2, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), batch_campaign_data=campaign_data)
    report = controller.run()
    assert report == {"mode": "batch", "configs": 2,
                      "campaign_root": str(tmp_path / "camp")}
    assert backend.batch_runs == [2]

    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT mode FROM campaign").fetchone()[0] == "batch"
    assert conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 1
    rows = dict(conn.execute("SELECT config_name, status FROM unit").fetchall())
    assert rows == {"ca": "passed", "cb": "failed"}  # FakeBackend alternates
    assert conn.execute("SELECT DISTINCT n_samples FROM unit").fetchall() == [(2,)]
    conn.close()
    store.close()


def test_store_strategy_state_roundtrip(tmp_path):
    store = CampaignStore(tmp_path / "s.db")
    cid = store.create_campaign("c", {"version": 1})
    store.save_strategy_state(cid, b"opaque-blob")
    assert store.load_strategy_state(cid) == b"opaque-blob"
    store.close()


def test_store_description_roundtrip(tmp_path):
    from robovast.common.store import read_campaign_description
    with CampaignStore(tmp_path / STORE_FILENAME) as store:
        store.create_campaign("c", {"version": 1}, description="pilot: 5 reps")
    assert read_campaign_description(tmp_path) == "pilot: 5 reps"


def test_store_without_description_reads_none(tmp_path):
    """No description is None, not an empty string — the same answer a store written
    before the column existed gives, so callers need one branch, not two."""
    from robovast.common.store import read_campaign_description
    with CampaignStore(tmp_path / STORE_FILENAME) as store:
        store.create_campaign("c", {"version": 1})
    assert read_campaign_description(tmp_path) is None


def test_description_read_from_pre_column_store_is_none(tmp_path):
    """A schema-v2 store (no ``description`` column) reads back as "no description"
    rather than raising — and is left unmigrated, since the read is read-only."""
    # Built from the frozen migrations, not ``_SCHEMA``: that constant is the *current*
    # layout and already has ``description``, so using it here would silently stop
    # simulating a v2 store while the assertion below still passed.
    from robovast.common.store import (_MIGRATION_ADD_RUN, _MIGRATION_INITIAL,
                                       read_campaign_description)
    db = tmp_path / STORE_FILENAME
    conn = sqlite3.connect(db)
    conn.executescript(_MIGRATION_INITIAL)
    conn.executescript(_MIGRATION_ADD_RUN)
    conn.execute("PRAGMA user_version = 2")
    conn.execute(
        "INSERT INTO campaign (id, name, mode, created_at) VALUES (1, 'old', 'batch', 0)")
    conn.commit()
    conn.close()

    assert read_campaign_description(tmp_path) is None
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 2


def test_fresh_store_stamps_schema_version(tmp_path):
    from robovast.common.store import SCHEMA_VERSION
    store = CampaignStore(tmp_path / "camp.db")
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store.close()


def test_pre_versioning_store_migrates_forward(tmp_path):
    """A store created before schema versioning (tables present, user_version 0)
    is adopted at the current version and stays readable."""
    from robovast.common.store import _MIGRATION_INITIAL, SCHEMA_VERSION
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    # The v1 layout as it shipped — a pre-versioning store cannot have today's columns.
    conn.executescript(_MIGRATION_INITIAL)
    conn.execute(
        "INSERT INTO campaign (id, name, mode, created_at) VALUES (1, 'old', 'search', 0)")
    conn.commit()
    conn.close()
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 0

    with CampaignStore(db) as store:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [c["name"] for c in store.list_campaigns()] == ["old"]


def _schema_fingerprint(conn):
    """Every table's ordered ``(column, type)`` list, plus index/view names."""
    out = {}
    for name, typ in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY name"):
        if typ == "table":
            out[name] = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{name}")')]
        else:
            out[f"{typ}:{name}"] = None
    return out


@pytest.mark.parametrize("start_version", [0, 1, 2, 3])
def test_fresh_and_migrated_schemas_match(tmp_path, start_version):
    """``_SCHEMA`` and the migration ladder must produce the *same* database.

    The two exist for different jobs — ``_SCHEMA`` so a reader can see the current
    layout, ``_MIGRATIONS`` so an existing store upgrades — which means they can drift:
    a column added to one and not the other yields two different databases depending on
    when the store happened to be created. Comparing ordered ``(column, type)`` per
    table makes that impossible to merge; it also pins the convention that a new column
    is appended to ``_SCHEMA`` in the same position the migration adds it.

    ``start_version`` 0 covers the pre-versioning store (tables present, no stamp).
    """
    from robovast.common.store import _MIGRATION_INITIAL, _MIGRATIONS, SCHEMA_VERSION

    with CampaignStore(tmp_path / "fresh.db") as store:
        fresh = _schema_fingerprint(store._conn)

    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript(_MIGRATION_INITIAL)
    for v in range(1, max(start_version, 1)):
        conn.executescript(_MIGRATIONS[v])
    if start_version:
        conn.execute(f"PRAGMA user_version = {start_version}")
    conn.commit()
    conn.close()

    with CampaignStore(old) as store:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _schema_fingerprint(store._conn) == fresh


def test_newer_store_is_read_best_effort(tmp_path):
    """A store written by a newer robovast (higher user_version, extra column) is
    not downgraded and remains readable through existing columns."""
    from robovast.common.store import _SCHEMA, SCHEMA_VERSION
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.execute("ALTER TABLE campaign ADD COLUMN future_col TEXT")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.execute(
        "INSERT INTO campaign (id, name, mode, created_at) VALUES (1, 'newer', 'search', 0)")
    conn.commit()
    conn.close()

    with CampaignStore(db) as store:
        # Untouched: still at the newer version, not migrated down.
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        assert [c["name"] for c in store.list_campaigns()] == ["newer"]


class _EvaluatorRaising:
    """Evaluates normally except for one param set, for which it raises *exc*.

    Stands in for an extractor that found nothing measurable in a cell (every run lost to
    container bringup), or for one with an outright bug -- the two cases the controller
    must treat differently.
    """

    def __init__(self, cfg, tmp_path, fail_x, exc):
        self._inner = Evaluator(cfg, str(tmp_path))
        self._fail_x = fail_x
        self._exc = exc

    def evaluate(self, config_dir, params):
        if params.values["x"] == self._fail_x:
            raise self._exc
        return self._inner.evaluate(config_dir, params)


def test_a_sample_less_cell_is_recorded_and_skipped_not_fatal(tmp_path):
    """A cell whose runs produced nothing measurable must not abort the campaign.

    The extractor is right to refuse both alternatives -- a fabricated 0.0 is
    indistinguishable from a cell that genuinely scored zero, and a bare raise discarded
    every completed batch over one cell's container bringup -- so the framework records it
    and carries on, exactly as it does for an unrealizable draw.

    Unlike ``composition_failed``, these runs HAPPENED, so they must still be recorded:
    the cell's failures stay visible instead of vanishing with the unusable evaluation.
    """
    from robovast.search.extractor import NoSampleError

    cfg = _cfg(batches=1, per_batch=3)
    param_sets = [ParamSet(values={"x": 0.1}),
                  ParamSet(values={"x": 0.2}),
                  ParamSet(values={"x": 0.3})]
    controller, store, _backend = _search_controller(
        cfg, tmp_path, strategy=_Fixed(cfg, param_sets),
        evaluator=_EvaluatorRaising(cfg, tmp_path, 0.2,
                                    NoSampleError("no measurable run", config_name="c")))

    report = controller.run()  # must not raise

    assert len(report.evaluations) == 2  # the unmeasurable cell is excluded
    assert {ev.params.values["x"] for ev in report.evaluations} == {0.1, 0.3}

    conn = sqlite3.connect(store.db_path)
    rows = conn.execute(
        "SELECT paramset_id, status, n_samples, objectives_json, result_dir FROM unit"
    ).fetchall()
    # The runs of the skipped cell are recorded, which is what separates this from
    # composition_failed (where nothing ever ran).
    run_counts = conn.execute(
        "SELECT u.status, COUNT(r.id) FROM unit u LEFT JOIN run r ON r.unit_id = u.id "
        "GROUP BY u.status").fetchall()
    conn.close()
    store.close()

    assert len(rows) == 3  # every param set recorded, including the unmeasurable one
    skipped = [r for r in rows if r[1] == "no_sample"]
    assert len(skipped) == 1
    assert skipped[0][0] == param_sets[1].id
    assert skipped[0][2] == 0                      # n_samples
    assert json.loads(skipped[0][3]) == {}         # no fabricated objective
    assert skipped[0][4] != ""                     # it HAS a result dir, unlike composition_failed
    assert dict(run_counts)["no_sample"] == 2      # its runs are recorded (runs=2)


def test_an_extractor_bug_still_aborts_the_campaign(tmp_path):
    """Only NoSampleError is tolerated. Any other exception is an extractor defect and
    must still abort -- swallowing it is how an objective goes structurally dead while
    the campaign reports success, which is the failure the typed error exists to avoid.
    """
    cfg = _cfg(batches=1, per_batch=3)
    param_sets = [ParamSet(values={"x": 0.1}),
                  ParamSet(values={"x": 0.2}),
                  ParamSet(values={"x": 0.3})]
    controller, store, _backend = _search_controller(
        cfg, tmp_path, strategy=_Fixed(cfg, param_sets),
        evaluator=_EvaluatorRaising(cfg, tmp_path, 0.2,
                                    KeyError("column the extractor assumed")))

    with pytest.raises(KeyError):
        controller.run()
    store.close()


# --- A restarted container invalidates its trial, not the campaign ---------------------

def _passing_run(config_dir, run_id):
    """A run that wrote a PASSING test.xml -- the dangerous case."""
    d = config_dir / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "test.xml").write_text(
        '<testsuite name="t" tests="1" failures="0" errors="0" time="1.0">'
        '<testcase name="c"/></testsuite>')
    return d


def _invalidate(campaign_root, *run_keys, job_dir="_jobs/batch-0/job-0"):
    from robovast.common.campaign_data import KIND_INVALID, record_intervention
    record_intervention(campaign_root, kind=KIND_INVALID, job_dir=job_dir,
                        job_name="rrroqs-x-0", source="runner",
                        detail="ContainerRestarted: container sut restarted 1x after "
                               "Error (exit 135, SIGBUS)",
                        runs=tuple(run_keys))


def test_an_invalidated_run_is_not_a_completed_sample(tmp_path):
    """n_samples is len(completed_run_dirs), so the cell must score over what is left."""
    from robovast.search.extractor import completed_run_dirs

    cfg = tmp_path / "cfg-1"
    _passing_run(cfg, 0)
    _passing_run(cfg, 1)
    _invalidate(tmp_path, "cfg-1/1")

    assert [d.name for d in completed_run_dirs(cfg)] == ["0"]


def test_invalid_overrides_a_passing_verdict(tmp_path):
    """The point of the whole change, as one assertion.

    A trial whose simulator crashed and restarted under it carried on against a process
    that had lost its state. When such a run writes `passed`, nothing else about it looks
    wrong -- which is precisely why the verdict must not be believed."""
    from robovast.common.campaign_data import read_run_outcomes

    cfg = tmp_path / "cfg-1"
    _passing_run(cfg, 0)
    _passing_run(cfg, 1)
    _invalidate(tmp_path, "cfg-1/1")

    by_id = {o["run_id"]: o for o in read_run_outcomes(cfg, tmp_path)}
    assert by_id[0]["status"] == "passed"
    assert by_id[1]["status"] == "invalid"
    assert by_id[1]["passed"] == 0
    assert "SIGBUS" in by_id[1]["failure_message"]


def test_the_discarded_verdict_is_still_on_disk(tmp_path):
    """Discarding is not destroying: the test.xml stays, so the override is reversible and
    anyone can check what was thrown away."""
    cfg = tmp_path / "cfg-1"
    run = _passing_run(cfg, 0)
    _invalidate(tmp_path, "cfg-1/0")
    assert (run / "test.xml").is_file()


def test_a_cell_whose_every_run_was_invalidated_has_no_sample(tmp_path):
    """Which is what makes the campaign survive: the search loop already records
    `no_sample` and carries on rather than aborting."""
    from robovast.search.extractor import completed_run_dirs

    cfg = tmp_path / "cfg-1"
    _passing_run(cfg, 0)
    _passing_run(cfg, 1)
    _invalidate(tmp_path, "cfg-1/0", "cfg-1/1")

    assert completed_run_dirs(cfg) == []


def test_invalid_is_not_counted_as_a_failure(tmp_path):
    """An infrastructure fault is not evidence against the system under test."""
    from robovast.execution.controller import _tally_outcomes

    outcomes = [{"status": "passed"}, {"status": "failed"}, {"status": "invalid"},
                {"status": "killed"}]
    assert _tally_outcomes(outcomes) == (1, 1, 1)


def test_aggregate_status_skips_invalidated_runs(tmp_path):
    """Neither a pass nor a strike: it is a measurement that was never made."""
    from robovast.common.campaign_data import aggregate_run_status

    cfg = tmp_path / "cfg-1"
    runs = [_passing_run(cfg, 0), _passing_run(cfg, 1)]
    assert aggregate_run_status(runs) == "passed"
    assert aggregate_run_status(runs, invalid={"cfg-1/1"}) == "passed"
    assert aggregate_run_status(runs, invalid={"cfg-1/0", "cfg-1/1"}) == "no_sample"


def test_a_config_dir_outside_its_campaign_degrades_honestly(tmp_path):
    """No ledger beside it means the old behaviour, not an error. Stated because it is a
    real limitation of resolving the campaign root from the config dir's parent."""
    from robovast.search.extractor import completed_run_dirs

    cfg = tmp_path / "loose" / "cfg-1"
    _passing_run(cfg, 0)
    assert len(completed_run_dirs(cfg)) == 1


def test_the_discarded_verdict_is_named_in_the_message(tmp_path):
    """Overriding a verdict silently would be its own kind of data loss. The run says which
    verdict it discarded, so a reader who remembers a passing run is told why it now reads
    `invalid` instead of having to go and find the ledger."""
    from robovast.common.campaign_data import read_run_outcomes

    cfg = tmp_path / "cfg-1"
    _passing_run(cfg, 0)
    (cfg / "1").mkdir(parents=True)
    (cfg / "1" / "test.xml").write_text(
        '<testsuite name="t" tests="1" failures="1" errors="0" time="1.0">'
        '<testcase name="c"><failure/></testcase></testsuite>')
    _invalidate(tmp_path, "cfg-1/0", "cfg-1/1")

    by_id = {o["run_id"]: o for o in read_run_outcomes(cfg, tmp_path)}
    assert "discarded verdict: passed" in by_id[0]["failure_message"]
    assert "discarded verdict: failed" in by_id[1]["failure_message"]


def test_an_invalidated_run_with_no_verdict_says_so_without_inventing_one(tmp_path):
    """A job deleted at grace_period_seconds=0 often never wrote a test.xml at all. The
    message then names the cause and stops, rather than claiming a verdict was discarded."""
    from robovast.common.campaign_data import read_run_outcomes

    cfg = tmp_path / "cfg-1"
    (cfg / "0").mkdir(parents=True)          # no test.xml
    _invalidate(tmp_path, "cfg-1/0")

    outcome, = read_run_outcomes(cfg, tmp_path)
    assert outcome["status"] == "invalid"
    assert "discarded verdict" not in outcome["failure_message"]
    assert "SIGBUS" in outcome["failure_message"]


def test_an_aborted_search_records_the_batches_it_completed(tmp_path):
    """A search that dies mid-loop records the rounds it finished, not zero.

    The count is kept on the controller rather than in a local of ``_run_search``, because
    the loop it counts runs in a callee: a local would still read 0 after ``_search_loop``
    raised, and the abort path would file that 0. The campaign that motivated this recorded
    `batches: 0` for 22 batches of completed work -- wrong in the confident direction,
    since it is indistinguishable from a search that never ran a batch at all.

    So the real loop runs here and the third batch raises from inside it. Anything that
    reintroduced the local would report 0 while two rounds sit in the store.
    """
    cfg = _cfg(batches=50, per_batch=2)      # a budget the loop cannot reach before it dies
    controller, store, _ = _search_controller(cfg, tmp_path)
    real_batch = controller._run_search_batch

    def _die_on_the_third(param_sets, batch_idx, batch_id):
        if batch_idx == 2:
            raise RuntimeError("2 scenario job(s) cannot start after 60s")
        return real_batch(param_sets, batch_idx, batch_id)

    controller._run_search_batch = _die_on_the_third
    campaign_id = store.create_campaign(name="c", config={}, mode="search", config_dir=".")
    with pytest.raises(RuntimeError):
        controller._run_search(campaign_id)

    conn = sqlite3.connect(store.db_path)
    batches, kind, reason = conn.execute(
        "SELECT batches, stop_kind, stop_reason FROM campaign").fetchone()
    # Three batch rows exist: `open_batch` runs at the top of each round, so the one that
    # died is opened too. That is exactly why the recorded count cannot be "rows opened" --
    # it is rounds *completed*, which is two, and it is published from inside the loop
    # rather than returned from it.
    assert conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 3
    assert batches == 2
    assert kind == "error"
    assert "cannot start" in reason
    conn.close()
    store.close()


def test_budget_is_published_before_the_first_batch(tmp_path):
    """A search reports its budget from the start, not from the end of round one.

    Every criterion used to be published only at the end of the loop, so a campaign spent
    its whole first batch reporting no budget at all -- and a batch is many runs long. The
    reader saw the runs bar alone, with nothing saying `0 / 50 batches`, during exactly the
    window in which the question is whether the search is going anywhere.

    Asserted by making the first batch *raise*, so the loop never completes a round: what
    is on the state afterwards can only have been published before it.
    """
    from robovast.execution.control_server import ControllerState

    cfg = _cfg(batches=50, per_batch=2)
    state = ControllerState()
    controller, store, _ = _search_controller(cfg, tmp_path)
    controller.state = state

    def _die(*_a, **_k):
        raise RuntimeError("the first batch never finished")

    controller._run_search_batch = _die
    campaign_id = store.create_campaign(name="c", config={}, mode="search", config_dir=".")
    with pytest.raises(RuntimeError):
        controller._run_search(campaign_id)

    budget = state.snapshot().budget
    assert [(b.label, b.current, b.limit, b.done) for b in budget] == [("batches", 0.0, 50.0, False)]
    # `kind` is what a reader keys on to find this row among criteria named after the
    # user's own metrics, so it has to be right from the first publish too.
    assert budget[0].kind == "batches"
    store.close()


def test_an_unmeasured_target_objective_publishes_no_number(tmp_path):
    """`target_objective` before any result is NaN, which is not JSON and is not zero.

    It reaches the wire as ``None`` so a reader renders `—`. Reporting 0 would read as a
    measured objective of zero -- the wrong answer in the confident direction, and on a
    minimizing search it would look like the target had already been met.
    """
    from robovast.execution.control_server import ControllerState
    from robovast.search.stopping import StopSnapshot, build_stop_conditions

    cfg = _cfg(batches=5, per_batch=2, stopping=[{"target_objective": 0.9}])
    state = ControllerState()
    controller, store, _ = _search_controller(cfg, tmp_path)
    controller.state = state

    controller._publish_budget(build_stop_conditions(cfg), StopSnapshot(batch=0, elapsed=0.0))

    by_kind = {b.kind: b for b in state.snapshot().budget}
    assert by_kind["target_objective"].current is None
    assert by_kind["batches"].current == 0.0
    store.close()
