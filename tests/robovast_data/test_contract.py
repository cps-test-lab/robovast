# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a reader addresses -- table names, column names and types -- is the data contract.

The fixture campaign's catalog, every table built, is kept as a golden file per contract
number. A change to it is a change every notebook, panel and agent prompt sees, so it fails
here until the contract number moves and the changelog names the tables; then
``ROBOVAST_UPDATE_CONTRACT=1 pytest tests/robovast_data/test_contract.py`` writes the new file.
"""

import json
import os
from pathlib import Path

from robovast.results_processing.data_query import describe_data_db
from robovast_data import DATA_CONTRACT, Engine, Scope

GOLDEN = Path(__file__).parent / "fixtures" / f"data_contract_{DATA_CONTRACT}.json"


def test_the_fixture_campaign_describes_as_the_contract_says(campaign):
    engine = Engine([Scope(str(campaign))], workers=1)
    engine.ensure(sorted(n for n, e in engine.catalog().items() if e["kind"] == "table"))
    described = describe_data_db(str(campaign))
    assert described["data_contract"] == DATA_CONTRACT
    got = {f"{t['schema']}.{t['table']}": t["columns"] for t in described["tables"]}
    if os.environ.get("ROBOVAST_UPDATE_CONTRACT"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(GOLDEN.read_text())
    changed = sorted(set(got) ^ set(want)) + sorted(k for k in got.keys() & want.keys()
                                                  if got[k] != want[k])
    assert not changed, (
        f"the columns a reader addresses changed in {changed}: move DATA_CONTRACT, name the "
        "tables in the changelog, then write the new golden with ROBOVAST_UPDATE_CONTRACT=1")
