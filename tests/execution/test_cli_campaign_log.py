# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast campaign log`` prints the campaign log's rows, pulled once or followed.

One rendering -- ``[PHASE] <time> <LEVEL> <logger>: <message>``, a continuation indented --
which the MCP tool reads through as well; ``--json`` prints the rows as the interface names
them. The filters reach the service's read, and ``--follow`` reads the service's stream rather
than polling.
"""

import contextlib
import time

from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.client.tail import format_campaign_log_row
from robovast.service.interface import CampaignLogChunk, CampaignLogRow

CID = "log-2026-09-01-101500"
_T = time.mktime(time.strptime("2026-09-01 10:15:00", "%Y-%m-%d %H:%M:%S"))

_ROWS = [
    CampaignLogRow(phase="BUILD", seq=0, message="#1 load"),
    CampaignLogRow(phase="RUN", seq=1, wall_ts=_T, level="ERROR",
                   logger="robovast.execution.controller", message="run 1 failed\nTraceback"),
]


class _Client:
    def __init__(self):
        self.asked = []

    def get_campaign_logs(self, campaign_id, cursor="", *, phase=None, min_level=None,
                          grep=None):
        self.asked.append(("pull", campaign_id, cursor, phase, min_level, grep))
        return CampaignLogChunk(rows=_ROWS, cursor="c-1", eof=True, phases=["BUILD", "RUN"])

    def iter_campaign_log(self, campaign_id, cursor="", *, phase=None, min_level=None,
                          grep=None):
        self.asked.append(("follow", campaign_id, cursor, phase, min_level, grep))
        yield CampaignLogChunk(rows=_ROWS[:1], cursor="c-1")
        yield CampaignLogChunk(rows=_ROWS[1:], cursor="c-2")
        yield CampaignLogChunk(cursor="c-2", eof=True)


def _run(monkeypatch, *args):
    client = _Client()

    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield client, "fake service"

    monkeypatch.setattr(campaign_cli, "service_client", _service)
    result = CliRunner().invoke(campaign_cli.campaign, ["log", CID, *args])
    assert result.exit_code == 0, result.output
    return client, result.output


def test_a_row_renders_its_phase_time_level_and_logger_with_continuations_indented():
    assert format_campaign_log_row(_ROWS[1]) == (
        "[RUN] 2026-09-01 10:15:00 ERROR robovast.execution.controller: run 1 failed\n"
        "    Traceback")
    assert format_campaign_log_row(_ROWS[0]) == "[BUILD] NOTE: #1 load"


def test_a_plain_read_pulls_once_and_prints_every_row(monkeypatch):
    client, out = _run(monkeypatch)
    assert client.asked == [("pull", CID, "", None, None, None)]
    assert out.endswith("[BUILD] NOTE: #1 load\n"
                        "[RUN] 2026-09-01 10:15:00 ERROR robovast.execution.controller: "
                        "run 1 failed\n    Traceback\n")


def test_the_filters_reach_the_read(monkeypatch):
    client, _out = _run(monkeypatch, "--phase", "run", "--min-level", "ERROR", "--grep", "fail")
    assert client.asked == [("pull", CID, "", "run", "ERROR", "fail")]


def test_follow_reads_the_stream_with_the_same_filters(monkeypatch):
    client, out = _run(monkeypatch, "--follow", "--phase", "run")
    assert client.asked == [("follow", CID, "", "run", None, None)]
    assert "[BUILD] NOTE: #1 load\n" in out and "run 1 failed" in out


def test_json_prints_one_object_per_row(monkeypatch):
    import json

    _client, out = _run(monkeypatch, "--json")
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert [r["message"] for r in rows] == ["#1 load", "run 1 failed\nTraceback"]
    assert rows[1]["level"] == "ERROR" and rows[1]["phase"] == "RUN"
