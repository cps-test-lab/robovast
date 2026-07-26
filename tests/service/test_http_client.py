# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the HTTP client's workspace-file routes.

``read_project_file`` and ``delete_project_file`` send the file's ``path`` as a
query param. The request helpers take the URL route as their first positional
arg, so that arg must not also be named ``path`` — otherwise
``self._delete(route, path=path)`` raises "multiple values for argument 'path'".
These build the request without touching the network (``requests`` is stubbed).
"""

import requests

from robovast.service.http_client import HTTPTransport


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"ok": True, "content": "x", "path": "a.vast"}


def _capture(monkeypatch):
    calls = {}

    def fake(method):
        def _call(url, params=None, timeout=None, **kw):
            calls[method] = {"url": url, "params": params}
            return _Resp()
        return _call

    monkeypatch.setattr(requests, "get", fake("get"))
    monkeypatch.setattr(requests, "delete", fake("delete"))
    return calls


def test_delete_project_file_sends_path_query_without_collision(monkeypatch):
    calls = _capture(monkeypatch)
    HTTPTransport("http://svc").delete_project_file("ws-1", "scenes/room.json")
    assert calls["delete"]["url"].endswith("/workspaces/ws-1/file")
    assert calls["delete"]["params"] == {"path": "scenes/room.json"}


def test_read_project_file_sends_path_query_without_collision(monkeypatch):
    calls = _capture(monkeypatch)
    HTTPTransport("http://svc").read_project_file("ws-1", "demo.vast")
    assert calls["get"]["url"].endswith("/workspaces/ws-1/file")
    assert calls["get"]["params"] == {"path": "demo.vast"}
