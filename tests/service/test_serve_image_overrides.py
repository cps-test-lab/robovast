# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast serve`` announces which project its images come from.

A persistent service pulling from a dev project, configured once at process startup, is
easy to forget about months later -- and "which images is it running?" is the first
question when a campaign behaves unexpectedly. See :func:`robovast.service.app.serve`.
"""

import logging


class _StubApp:
    """Just enough app for ``serve`` -- it only reads/writes ``app.state``."""

    class _State:
        pass

    def __init__(self):
        self.state = self._State()


class _FakeServer:
    """``uvicorn.Server`` stand-in that returns from ``run()`` immediately."""

    def __init__(self, config):
        pass

    def handle_exit(self, sig, frame):
        pass

    def run(self):
        pass


def test_serve_logs_the_image_project(monkeypatch, caplog):
    """Logged unconditionally, and marked when it is not the built-in project.

    Unconditionally because a line that appears only when someone configured something
    cannot answer "which images is this running?" -- the case where nobody remembers
    configuring anything is exactly the case worth logging.
    """
    import uvicorn

    from robovast.service import app as app_module

    monkeypatch.setenv("ROBOVAST_PROJECT", "freeedlabs")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "dev")
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(app_module, "build_app",
                        lambda impl, mount_mcp=True, auth_token=None: _StubApp())
    with caplog.at_level(logging.INFO, logger="robovast.service.app"):
        app_module.serve(impl=object())
    assert "RoboVAST image default: freeedlabs/*:dev (ROBOVAST_PROJECT)" in caplog.text
