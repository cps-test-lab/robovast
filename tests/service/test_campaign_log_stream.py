# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign log's routes: rows after a cursor, pulled or streamed.

``GET /campaigns/{id}/logs`` answers one read; ``.../logs/stream`` pushes the same read as
server-sent events -- each frame a JSON array of rows with ``id`` set to the cursor to resume
from, ``eof`` once the campaign is over, and an error the read raised as ``streamerror`` rather
than a broken connection. The client side of that stream is read here too, so the framing the
service writes and the framing the CLI's follow reads are held to each other.
"""

import json
import threading
import time
import os

from robovast.common.campaign_logs import EXECUTION_DIR
from robovast.service.app import build_app
from tests.service.null_service import NullService
from robovast.service.http_client import HTTPTransport
from robovast.service.interface import Routes, ServiceError
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

#: Long enough for the stream's loop to deliver its frames, short enough that a stream which
#: never sends one fails the test rather than hanging it.
_FRAME_BUDGET_S = 5

_CAMPAIGN = "rows-2026-01-01-000000"

_LOG = ("2026-01-01 00:00:01 INFO robovast.execution.controller: batch 0\n"
        "2026-01-01 00:00:02 ERROR robovast.execution.controller: run 1 failed\n"
        "Traceback\n"
        "2026-01-01 00:00:03 INFO robovast.execution.controller: batch 1\n")


def _app_with_log(tmp_path, build_log=""):
    results = tmp_path / "results"
    exec_dir = results / _CAMPAIGN / EXECUTION_DIR
    exec_dir.mkdir(parents=True)
    (exec_dir / "controller.log").write_text(_LOG, encoding="utf-8")
    if build_log:
        (exec_dir / "build.log").write_text(build_log, encoding="utf-8")
    past = time.time() - 60
    for path in exec_dir.iterdir():
        os.utime(path, (past, past))
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return build_app(NullService(store=store, results_dir=str(results)))


def _stream(app, path, headers=None):
    """The bytes one connection produces before the app is asked to shut down."""
    from fastapi.testclient import TestClient

    timer = threading.Timer(_FRAME_BUDGET_S, lambda: setattr(app.state, "should_exit",
                                                             lambda: True))
    timer.start()
    try:
        with TestClient(app) as client:
            with client.stream("GET", path, headers=headers or {}) as response:
                assert response.headers["content-type"].startswith("text/event-stream")
                return "".join(response.iter_text())
    finally:
        timer.cancel()


def _frames(body):
    """``[(event, id, data)]`` of an SSE body, comments dropped."""
    out = []
    for block in body.split("\n\n"):
        fields = {"event": "message", "id": "", "data": []}
        for line in block.splitlines():
            if line.startswith(":") or not line:
                continue
            key, _, value = line.partition(": ")
            if key == "data":
                fields["data"].append(value)
            else:
                fields[key] = value
        if fields["data"]:
            out.append((fields["event"], fields["id"], "\n".join(fields["data"])))
    return out


# -- the pull ---------------------------------------------------------------------------

def test_the_route_answers_rows_and_a_cursor_to_continue_from(tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(_app_with_log(tmp_path)) as client:
        body = client.get(Routes.campaign_logs(_CAMPAIGN)).json()
        assert [r["message"] for r in body["rows"]] == ["batch 0", "run 1 failed\nTraceback",
                                                        "batch 1"]
        assert body["rows"][1]["level"] == "ERROR" and body["rows"][1]["phase"] == "RUN"
        assert body["eof"] is True and body["phases"] == ["RUN"]
        again = client.get(Routes.campaign_logs(_CAMPAIGN), params={"cursor": body["cursor"]})
        assert again.json()["rows"] == [] and again.json()["eof"] is True


def test_the_filters_are_query_parameters(tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(_app_with_log(tmp_path, build_log="#1 load\n")) as client:
        body = client.get(Routes.campaign_logs(_CAMPAIGN),
                          params={"phase": "run", "min_level": "error"}).json()
        assert [r["message"] for r in body["rows"]] == ["run 1 failed\nTraceback"]
        assert body["phases"] == ["BUILD", "RUN"]
        assert client.get(Routes.campaign_logs(_CAMPAIGN),
                          params={"phase": "nope"}).status_code == 400


# -- the stream -------------------------------------------------------------------------

def test_the_stream_frames_rows_with_the_cursor_as_id_and_ends_on_eof(tmp_path):
    body = _stream(_app_with_log(tmp_path), Routes.campaign_logs_stream(_CAMPAIGN))
    frames = _frames(body)
    assert [event for event, _id, _data in frames] == ["message", "eof"]
    event, cursor, data = frames[0]
    assert [r["message"] for r in json.loads(data)] == ["batch 0", "run 1 failed\nTraceback",
                                                        "batch 1"]
    assert cursor, "the frame carries no cursor to resume from"


def test_a_stream_resumed_by_last_event_id_sends_only_what_follows(tmp_path):
    app = _app_with_log(tmp_path)
    first = _frames(_stream(app, Routes.campaign_logs_stream(_CAMPAIGN)))[0]
    resumed = _frames(_stream(app, Routes.campaign_logs_stream(_CAMPAIGN),
                              headers={"Last-Event-ID": first[1]}))
    assert [event for event, _id, _data in resumed] == ["eof"]


def test_the_stream_takes_the_same_filters_as_the_pull(tmp_path):
    body = _stream(_app_with_log(tmp_path),
                   Routes.campaign_logs_stream(_CAMPAIGN) + "?min_level=error")
    _event, _cursor, data = _frames(body)[0]
    assert [r["message"] for r in json.loads(data)] == ["run 1 failed\nTraceback"]


def test_a_read_the_service_refuses_is_a_streamerror_not_a_dropped_connection(tmp_path):
    body = _stream(_app_with_log(tmp_path),
                   Routes.campaign_logs_stream(_CAMPAIGN) + "?phase=nope")
    frames = _frames(body)
    assert [event for event, _id, _data in frames] == ["streamerror", "eof"]
    assert "unknown phase" in frames[0][2]


# -- the client's side of the stream ----------------------------------------------------

class _StreamResp:
    """A streamed response, as ``requests`` hands one to ``iter_lines``."""

    ok, status_code, headers, url = True, 200, {}, "http://svc/x"

    def __init__(self, text):
        self._text = text

    def iter_lines(self, decode_unicode=True):
        del decode_unicode
        return iter(self._text.split("\n"))

    def close(self):
        pass


def _client(monkeypatch, text):
    transport = HTTPTransport("http://svc")
    seen = {}

    def _get(url, params=None, timeout=None, stream=False, headers=None):
        seen.update(url=url, params=params, headers=headers, stream=stream)
        return _StreamResp(text)

    monkeypatch.setattr(transport.session, "get", _get)
    return transport, seen


def test_the_client_reads_rows_and_carries_the_cursor_from_the_frame_id(monkeypatch):
    rows = json.dumps([{"phase": "RUN", "seq": 0, "level": "INFO", "message": "one"}])
    transport, seen = _client(monkeypatch, f": open\n\nid: c-1\ndata: {rows}\n\n"
                                           "event: heartbeat\ndata: {}\n\n"
                                           "event: eof\ndata: {}\n\n")
    chunks = list(transport.iter_campaign_log(_CAMPAIGN, "c-0", phase="run", grep="o"))
    assert [(c.cursor, c.eof, [r.message for r in c.rows]) for c in chunks] == [
        ("c-1", False, ["one"]), ("c-1", True, [])]
    assert seen["headers"]["Last-Event-ID"] == "c-0"
    assert seen["params"] == {"phase": "run", "grep": "o"}
    assert seen["url"].endswith(Routes.campaign_logs_stream(_CAMPAIGN))


def test_the_client_raises_the_services_sentence_on_a_streamerror(monkeypatch):
    transport, _seen = _client(monkeypatch, 'event: streamerror\ndata: "unknown phase"\n\n'
                                            "event: eof\ndata: {}\n\n")
    try:
        list(transport.iter_campaign_log(_CAMPAIGN, phase="nope"))
    except ServiceError as exc:
        assert "unknown phase" in str(exc)
    else:
        raise AssertionError("a streamerror must not read as an empty log")
