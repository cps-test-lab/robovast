# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A handler that fails takes only its own tables with it, not its class's.

A plan holds several handlers of one class -- one ``TopicTable`` per ``to_csv`` entry, one
``ActionTopics`` per action. A failure is recorded against the handler instance, so its
siblings keep their rows and each failure keeps its own reason.
"""

from robovast_decode.decode import decode_bag
from robovast_decode.handlers import TopicTable
from tests.robovast_decode.conftest import FIXTURE


def test_a_failing_handler_does_not_fail_its_siblings():
    good = TopicTable(["/collision"])
    bad = TopicTable(["/scan"])

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    bad.message = _boom
    report = decode_bag(str(FIXTURE / "0" / "rosbag2"), [good, bad])

    assert bad in report.failed and "boom" in report.failed[bad]
    assert good not in report.failed
    assert good.buffers["rosbag2_collision"].count > 0
