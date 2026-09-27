# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A query finds a table stale once the definitions sidecar that decides it changed.

A run's container writes the sidecar after its verdict and after the recorder closed the
bag, so a table can be final -- complete -- before the definitions it needed exist.
"""

import json
import shutil

from robovast_data import Engine, Scope
from robovast_decode.definitions import SIDECAR_NAME
from tests.robovast_decode.conftest import string_cdr, write_bag

from .conftest import nav_campaign

TOPICS = {"/opaque": ("example_msgs/msg/Opaque", [string_cdr(t) for t in "abc"])}
DEFINITIONS = {"example_msgs/msg/Opaque": "string data\n"}


def test_a_sidecar_that_lands_after_a_complete_build_is_read_by_the_next_query(tmp_path):
    campaign = nav_campaign(tmp_path / "nav-2026-01-01-00000000")
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    bag_dir = write_bag(campaign / "cfg" / "0" / "rosbag2", TOPICS)
    engine = Engine([Scope(str(campaign))], workers=1)
    with engine.execute("SELECT count(*) FROM rosbag2_opaque") as (con, problems):
        assert con.fetchone()[0] == 0
    assert problems and "/opaque" in str(problems[0])

    (bag_dir / SIDECAR_NAME).write_text(json.dumps(DEFINITIONS))
    with engine.execute("SELECT count(*) FROM rosbag2_opaque") as (con, problems):
        assert con.fetchone()[0] == 3
    assert not problems
    with engine.execute("SELECT reason FROM _recording WHERE topic = '/opaque'") as (con, _):
        assert con.fetchone()[0] is None, "the recording's report is read again too"
