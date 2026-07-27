# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for how the HTTP client builds file URLs.

The contract of the address space is that the string a caller passes **is** the URL
that serves it, so these assert the client appends the address to the base URL and
adds nothing to the path. That also retires an older bug class: the file path used to
travel as a ``path`` query param, which collided with the request helpers' own first
positional argument (``self._delete(route, path=path)`` → "multiple values for
argument 'path'"). It is now a path segment, so there is nothing to collide with.
"""

import requests

from robovast.service.http_client import HTTPTransport
from robovast.service.interface import EditFileRequest, WriteFileRequest


class _Resp:
    def __init__(self):
        self.content = b"bytes"

    def raise_for_status(self):
        pass

    def json(self):
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

    for method in ("get", "put", "post", "delete"):
        monkeypatch.setattr(requests, method, fake(method))
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
