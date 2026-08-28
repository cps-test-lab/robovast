# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for how the HTTP client builds file URLs.

The contract of the address space is that the string a caller passes **is** the URL
that serves it, so these assert the client appends the address to the base URL and
adds nothing to the path. That also rules out a bug class: a file path travelling as a
``path`` query param collides with the request helpers' own first positional argument
(``self._delete(route, path=path)`` → "multiple values for argument 'path'"). As a path
segment there is nothing to collide with.
"""

import pytest
import requests

from robovast.service.http_client import HTTPTransport
from robovast.service.interface import EditFileRequest, ServiceError, WriteFileRequest


class _Resp:
    """A successful response, shaped like the ``requests`` one the client now inspects.

    ``ok``/``status_code`` matter because the transport does not delegate to
    ``requests.Response.raise_for_status`` — it reads the status itself so it can carry
    the service's ``{detail}`` through instead of discarding it.
    """

    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.content = b"bytes"
        self.status_code = status_code
        self.ok = status_code < 400
        self.reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                       422: "Unprocessable Entity", 502: "Bad Gateway"}[status_code]
        self.url = "http://svc/x"
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        if self.text:
            # A proxy or a crashed worker answers with HTML, and ``requests`` raises
            # here rather than returning None — the branch that falls back to the body.
            raise ValueError("not JSON")
        # One permissive payload standing in for every response model these tests touch:
        # the assertions are about the *request* (url, params, timeout), so the body only
        # has to validate.
        return {"ok": True, "address": "/sources/ws-1/a.vast", "path": "a.vast",
                "entries": [], "content": "x",
                "campaign_id": "camp-1", "tables": [], "plots": [],
                "source": "object-store", "fetch_required": True, "cached": False,
                "transfer": "port-forward"}


def _capture(monkeypatch):
    calls = {}

    def fake(method):
        def _call(url, params=None, timeout=None, json=None, **kw):
            calls[method] = {"url": url, "params": params, "json": json,
                             "timeout": timeout}
            return _Resp()
        return _call

    # Patched on the Session, not the module: every request goes through one now,
    # which is what carries the credentials to the routes that build their own
    # request rather than using the verb helpers.
    for method in ("get", "put", "post", "delete"):
        monkeypatch.setattr(requests.Session, method,
                            lambda self, *a, _m=method, **kw: fake(_m)(*a, **kw))
    return calls


def test_read_file_gets_the_address_verbatim(monkeypatch):
    calls = _capture(monkeypatch)
    HTTPTransport("http://svc").read_file("/results/camp-1/_execution/outcome.json")
    assert calls["get"]["url"] == "http://svc/results/camp-1/_execution/outcome.json"
    # The text view is requested explicitly; a bare GET of the same URL is the bytes.
    assert calls["get"]["params"]["as"] == "text"


def test_read_file_bytes_asks_for_no_representation(monkeypatch):
    calls = _capture(monkeypatch)
    data = HTTPTransport("http://svc").read_file_bytes("/results/camp-1/nav/0/scene.bin")
    assert calls["get"]["url"] == "http://svc/results/camp-1/nav/0/scene.bin"
    assert calls["get"]["params"] is None
    assert data == b"bytes"


def test_list_files_is_the_directory_address(monkeypatch):
    calls = _capture(monkeypatch)
    HTTPTransport("http://svc").list_files("/results/camp-1/", recursive=True)
    assert calls["get"]["url"] == "http://svc/results/camp-1/"
    assert calls["get"]["params"]["recursive"] == 1


def test_write_and_edit_and_delete_use_the_same_url(monkeypatch):
    calls = _capture(monkeypatch)
    client = HTTPTransport("http://svc")
    address = "/sources/ws-1/scenes/room.osc"
    client.write_file(WriteFileRequest(address=address, content="x"))
    client.edit_file(EditFileRequest(address=address, old_string="a", new_string="b"))
    client.delete_file(address)
    for method in ("put", "post", "delete"):
        assert calls[method]["url"] == f"http://svc{address}", method
    assert calls["put"]["json"] == {"content": "x"}
    assert calls["post"]["json"] == {"old_string": "a", "new_string": "b"}


def test_data_calls_outlast_a_cold_object_store_fetch(monkeypatch):
    """A cluster campaign's first data call fetches its databases *inside* the request.

    At the default 30 s the client aborted while the service was still transferring, so a
    first query on a large campaign surfaced as a ReadTimeout indistinguishable from a
    broken service — the web UI never hit it only because ``fetch`` sets no timeout at all.
    """
    calls = _capture(monkeypatch)
    client = HTTPTransport("http://svc")

    client.describe_campaign_data("camp-1")
    assert calls["get"]["timeout"] == HTTPTransport.DATA_TIMEOUT

    client.query_campaign_data_sql("camp-1", "SELECT 1")
    assert calls["post"]["timeout"] == HTTPTransport.DATA_TIMEOUT

    client.list_campaign_plots("camp-1")
    assert calls["get"]["timeout"] == HTTPTransport.DATA_TIMEOUT


def test_the_readiness_probe_keeps_the_default_timeout(monkeypatch):
    """It is the cheap pre-flight: if *it* hangs, the service is unwell, not busy."""
    calls = _capture(monkeypatch)

    HTTPTransport("http://svc", timeout=7.0).campaign_data_status("camp-1")

    assert calls["get"]["url"] == "http://svc/campaigns/camp-1/data-status"
    assert calls["get"]["timeout"] == 7.0


# -- what the service said must survive the trip -----------------------------
#
# ``requests.Response.raise_for_status()`` reports the status line and the URL and drops
# the body. Every refusal this service writes to be acted on -- the binary-file advice,
# "stop it first", the address hint -- would reach an MCP tool or the CLI as
# ``"400 Client Error: Bad Request for url: ...?as=text&lines=200"``. The web UI parses
# ``detail`` itself, so the two client families would disagree about what the same call
# said.


def _refusing(monkeypatch, status: int, payload=None, text: str = ""):
    def _call(url, params=None, timeout=None, json=None, **kw):
        return _Resp(status_code=status, payload=payload, text=text)
    for method in ("get", "put", "post", "delete"):
        monkeypatch.setattr(requests.Session, method,
                            lambda self, *a, **kw: _call(*a, **kw))


def test_a_refusal_carries_the_services_own_message(monkeypatch):
    advice = ("rosbag2_0.mcap is a binary file — read it as bytes (GET the address "
              "without 'as=text'), or download the campaign archive.")
    _refusing(monkeypatch, 400, {"detail": advice})

    with pytest.raises(ServiceError) as excinfo:
        HTTPTransport("http://svc").read_file("/results/camp-1/rosbag2_0.mcap")

    assert str(excinfo.value) == advice
    assert excinfo.value.status == 400
    assert "Client Error" not in str(excinfo.value)


def test_a_refusal_is_still_an_oserror(monkeypatch):
    """``requests.HTTPError`` was one (via ``IOError``), and callers rely on that.

    ``data_access._REPORTED`` catches ``OSError`` to turn a service-side SQL rejection
    into a reported error rather than a traceback; changing the exception's base would
    have silently converted those into crashes.
    """
    _refusing(monkeypatch, 400, {"detail": "no such column: nope"})
    with pytest.raises(OSError):
        HTTPTransport("http://svc").query_campaign_data_sql("camp-1", "SELECT nope")


def test_a_validation_error_is_rendered_not_repr_d(monkeypatch):
    """FastAPI sends 422 as a list of per-field dicts, which str()s into noise."""
    _refusing(monkeypatch, 422, {"detail": [
        {"loc": ["body", "runs"], "msg": "Input should be a valid integer"}]})

    with pytest.raises(ServiceError) as excinfo:
        HTTPTransport("http://svc").get_status("camp-1")

    assert "body.runs: Input should be a valid integer" in str(excinfo.value)


def test_a_non_json_body_still_says_something(monkeypatch):
    """A proxy or a crash can answer with HTML; the status alone is not an answer."""
    _refusing(monkeypatch, 502, text="<html>Bad Gateway</html>")

    with pytest.raises(ServiceError) as excinfo:
        HTTPTransport("http://svc").version()

    assert "Bad Gateway" in str(excinfo.value)
    assert excinfo.value.status == 502


def test_byte_reads_report_the_detail_too(monkeypatch):
    """read_file_bytes bypasses the request helpers, so it needed the same treatment."""
    _refusing(monkeypatch, 404, {"detail": "no file at '/results/camp-1/nope.bin'"})

    with pytest.raises(ServiceError) as excinfo:
        HTTPTransport("http://svc").read_file_bytes("/results/camp-1/nope.bin")

    assert "no file at" in str(excinfo.value)
