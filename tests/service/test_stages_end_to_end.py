# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A search in stages, end to end and in-process: one ``.vast``, run by ``run_search_campaign``.

Everything is the real path a service takes -- the file is loaded and validated, every stage's
strategy is built at launch, cells are composed by the real ``Compose`` through the
``ParameterVariationList`` plugin, scored by a local file extractor, and the answers and regions
are applied between the stages by :mod:`robovast.search.chain` -- except the runs: a backend
writes each run's ``test.xml`` and the cell's composed parameters beside it, and the extractor
scores the cell from those, so every answer and region is known in advance.

The record is then read the ways a person reads it: the store, ``GET /search/history`` and
``describe_campaign_data`` through the test suite's service, the status recovered from disk, the
status report the CLI and the MCP print.
"""

# pylint: disable=protected-access

import json
import os
import sqlite3
import textwrap

from fastapi.testclient import TestClient

from robovast.client.campaign_report import status_to_dict
from robovast.common.common import load_config
from robovast.common.config import validate_config
from robovast.common.store import STORE_FILENAME
from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.controller import run_search_campaign
from robovast.execution.status_recovery import reconstruct_status_from_disk
from robovast.service.app import build_app
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_service import NullService

#: A scenario declaring the three parameters the search writes; nothing runs it here.
SCENARIO = textwrap.dedent("""\
    import osc.helpers

    scenario test_scenario:
        timeout(30s)

        width: string
        speed: string
        form: string

        do serial:
            wait elapsed(1s)
    """)

#: The cell's margin: a wider gap is safer, a faster person less so, a box a little more so.
EXTRACTOR = textwrap.dedent('''\
    import json
    from robovast.search.extractor import Extractor, ExtractResult, completed_run_dirs

    class Margin(Extractor):
        def extract(self, config_dir):
            runs = completed_run_dirs(config_dir)
            cell = json.loads((runs[0] / "cell.json").read_text())
            margin = (cell["width"] - 4.0 - 2.0 * cell["speed"]
                      + (0.5 if cell["form"] == "box" else 0.0))
            return ExtractResult(objectives={"margin": margin},
                                 measures={"success": margin > -1.0})
    ''')


def _vast(human_region):
    return textwrap.dedent(f"""\
        version: 7
        metadata:
          name: stages-e2e
        execution:
          containers: {{scenario: {{image: ghcr.io/cps-test-lab/robovast:latest}}}}
          runs: 1
          scenario_file: scenario.osc
        search:
          search_space:
            width: {{type: float, low: 0.0, high: 10.0}}
            speed: {{type: float, low: 0.0, high: 2.0, initial: 0.0}}
            form:  {{type: choice, values: [rod, box], initial: rod}}
          variations:
          - ParameterVariationList:
              scenario: [width, speed, form]
              values: [[$width, $speed, $form]]
          extract: {{plugin: search/margin.py:Margin}}
          objectives: [{{name: margin, direction: minimize}}]
          per_batch: 6
          seed: 7
          stages:
          - name: obstacle            # where does the gap start to matter?
            over: [width]
            strategy: boundary
            strategy_parameters: {{level: 0.0}}
            budget: [{{batches: 3}}]
            region: {{band: 1.5, min_cells: 3}}
          - name: refine              # the hardest gap that still succeeds, within that region
            over: [width]
            within: obstacle
            strategy: optuna
            budget: [{{batches: 2}}]
            answer: {{select: feasible-min, feasible: success}}
          - name: human               # how fast may a person walk past that gap?
            over: [speed]
            strategy: halton
            budget: [{{batches: 2}}]
            answer: {{select: worst, feasible: success}}
            region: {human_region}
          - name: decision            # every form, on the point the line built
            over: [form]
            strategy: grid
        """)


class _Backend(ExecutionBackend):
    """Runs nothing: writes each run's verdict and the cell's composed parameters."""

    def __init__(self):
        self.batches = []

    def run_batch(self, campaign_data, *, campaign_root, batch_tag, runs, options):
        self.batches.append((batch_tag, len(campaign_data["configs"])))
        for cfg in campaign_data["configs"]:
            for run in range(runs):
                run_dir = os.path.join(campaign_root, cfg["name"], str(run))
                os.makedirs(run_dir, exist_ok=True)
                with open(os.path.join(run_dir, "test.xml"), "w") as f:
                    f.write('<testsuite errors="0" failures="0" tests="1">'
                            '<testcase name="t" time="1.0"/></testsuite>')
                with open(os.path.join(run_dir, "cell.json"), "w") as f:
                    json.dump({k: cfg["config"][k] for k in ("width", "speed", "form")}, f)

    def finalize_campaign(self, campaign_root):
        pass


def _run(tmp_path, human_region):
    project = tmp_path / "project"
    (project / "search").mkdir(parents=True)
    (project / "scenario.osc").write_text(SCENARIO)
    (project / "search" / "margin.py").write_text(EXTRACTOR)
    vast = project / "stages.vast"
    vast.write_text(_vast(human_region))
    config = validate_config(load_config(str(vast)))
    results = tmp_path / "results"
    backend = _Backend()
    report = run_search_campaign(str(vast), config, str(results), runs=None, backend=backend,
                                 options=RunOptions(), campaign_id="stages-20260101-000000")
    return results / "stages-20260101-000000", report, backend


def _rows(campaign_dir, sql):
    con = sqlite3.connect(campaign_dir / STORE_FILENAME)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def _service(tmp_path, campaign_dir):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    service = NullService(store=store)
    service._campaigns_root = lambda: campaign_dir.parent
    return TestClient(build_app(service))


def test_a_line_of_four_stages_narrows_carries_a_cell_and_runs_to_its_end(tmp_path):
    campaign_dir, report, _ = _run(tmp_path, "{band: 10.0, min_cells: 2, level: 0.0}")
    obstacle, refine, human, decision = _rows(campaign_dir, "SELECT * FROM stage ORDER BY idx")
    assert [s["outcome"] for s in (obstacle, refine, human, decision)] == ["done"] * 4

    # The narrowing: refine searched obstacle's region, a box inside the declared range.
    region = json.loads(obstacle["region_json"])
    box = region["box"]["width"]
    assert json.loads(refine["space_json"]) == region["box"]
    assert 0.0 <= box["low"] < box["high"] <= 10.0 and (box["low"], box["high"]) != (0.0, 10.0)

    # The carried cell: human and decision stand on the width refine selected.
    width = json.loads(refine["answer_json"])["values"]["width"]
    assert json.loads(human["point_json"]) == {"width": width, "form": "rod"}
    speed = json.loads(human["answer_json"])["values"]["speed"]
    assert json.loads(decision["point_json"]) == {"width": width, "speed": speed}
    cells = _rows(campaign_dir, "SELECT u.params_json, u.status FROM unit u JOIN batch b "
                                "ON u.batch_id = b.id JOIN stage s ON b.stage_id = s.id "
                                "WHERE s.name = 'decision'")
    assert sorted(json.loads(c["params_json"])["form"] for c in cells) == ["box", "rod"]
    assert report.extra["stop"]["kind"] == "exhausted"

    with _service(tmp_path, campaign_dir) as client:
        answer = client.get("/campaigns/stages-20260101-000000/search/history")
        assert answer.status_code == 200, answer.text
        history = answer.json()
        described = client.get("/campaigns/stages-20260101-000000/describe")
    assert [s["name"] for s in history["stages"]] == ["obstacle", "refine", "human", "decision"]
    assert {b["stage"] for b in history["batches"]} == {"obstacle", "refine", "human",
                                                        "decision"}
    assert described.status_code == 200, described.text
    tables = {f"{t['schema']}.{t['table']}": t for t in described.json()["tables"]}
    assert "campaign.stage" in tables
    assert any(c.startswith("stage ") for c in tables["main.run_view"]["columns"])


def test_a_stage_whose_region_holds_too_few_cells_ends_the_line_there(tmp_path):
    campaign_dir, report, backend = _run(tmp_path, "{band: 0.0001, min_cells: 5, level: 0.0}")
    stages = _rows(campaign_dir, "SELECT name, outcome, region_json FROM stage ORDER BY idx")
    assert [(s["name"], s["outcome"]) for s in stages] == [
        ("obstacle", "done"), ("refine", "done"), ("human", "condition"),
        ("decision", "not_run")]
    assert json.loads(stages[2]["region_json"])["rule"]["min_cells"] == 5
    assert report.extra["stop"]["kind"] == "stage_condition"
    assert report.extra["stop"]["not_run"] == ["decision"]
    assert not any(tag for tag, _ in backend.batches if tag.startswith("batch-7"))

    status = reconstruct_status_from_disk(campaign_dir)
    shown = status_to_dict("stages-20260101-000000", "service", status)
    assert (shown["ended_at_stage"], shown["stage_rule"], shown["stages_not_run"]) == (
        "human", "region", ["decision"])
