# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""``get_simulation_screenshot`` returns the image, and where the service keeps it.

An image that exists only inline in a reply cannot be attached, saved or handed on, so the
tool also returns the ``url`` the kept render is served at -- built the way ``read_file``
builds its own, and omitted rather than guessed when there is nothing to point at.
"""

import json

from mcp.types import ImageContent, TextContent

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import results
from robovast.service import screenshot
from robovast.service.interface import ScreenshotFrame

NAME = "b" * 32 + ".png"


class _Client:
    base_url = "http://svc"

    def __init__(self, tmp_path, name):
        self._tmp_path = tmp_path
        self._name = name

    def campaign_screenshot(self, campaign_id, config_name, run_id, **kw):
        del campaign_id, config_name, run_id, kw
        frame = self._tmp_path / "robovast-screenshot-x" / "render" / "frame.png"
        frame.parent.mkdir(parents=True)
        frame.write_bytes(b"png")
        return ScreenshotFrame(path=str(frame), name=self._name)


def _call(monkeypatch, client):
    monkeypatch.setattr(service_access, "service_client", lambda: client)
    return results.get_simulation_screenshot("camp-1", "cfg")


def test_the_image_comes_with_the_url_it_is_kept_at(monkeypatch, tmp_path):
    result = _call(monkeypatch, _Client(tmp_path, NAME))
    image, kept = result.content
    assert isinstance(image, ImageContent) and image.mimeType == "image/png"
    assert isinstance(kept, TextContent)
    assert json.loads(kept.text) == {
        "url": f"http://svc/campaigns/camp-1/screenshots/{NAME}",
        "kept_for_s": screenshot.KEEP_S}
    # The caller's transient copy is still removed.
    assert not (tmp_path / "robovast-screenshot-x").exists()


def test_a_render_nobody_kept_returns_the_image_alone(monkeypatch, tmp_path):
    result = _call(monkeypatch, _Client(tmp_path, ""))
    assert [type(c) for c in result.content] == [ImageContent]


def test_no_origin_means_no_url(monkeypatch, tmp_path):
    """In-process with no declared origin there is no URL to hand out, so none is."""
    client = _Client(tmp_path, NAME)
    client.base_url = ""
    monkeypatch.setattr(service_access, "_declared_base", lambda c: "")
    result = _call(monkeypatch, client)
    assert [type(c) for c in result.content] == [ImageContent]
