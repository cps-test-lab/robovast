# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A reader told what a table holds hears that a topic stopped decoding, and for which run."""

import shutil

from robovast.results_processing.data_query import describe_data_db
from robovast_data import Engine, Scope
from tests.robovast_decode.conftest import UNDECODABLE_TOPICS, write_bag

from .conftest import nav_campaign


def test_describe_and_a_query_report_a_table_cut_short_by_an_undecodable_topic(tmp_path):
    campaign = nav_campaign(tmp_path / "nav-2026-01-01-00000000")
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    write_bag(campaign / "cfg" / "0" / "rosbag2", UNDECODABLE_TOPICS)
    engine = Engine([Scope(str(campaign))], workers=1)
    with engine.execute("SELECT count(*) FROM rosbag2_torn") as (con, problems):
        assert con.fetchone()[0] == 1
    (problem,) = problems
    assert problem.partial and "/torn" in str(problem) and "incomplete" in str(problem)

    tables = {t["table"]: t for t in describe_data_db(str(campaign))["tables"]}
    torn, fine = tables["rosbag2_torn"], tables["rosbag2_fine"]
    assert torn["rows"] == 1 and torn["built"] == 1
    assert "does not decode" in torn["failed"]["nav-2026-01-01-00000000/cfg/0"]
    assert "failed" not in fine
