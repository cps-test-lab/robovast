# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast serve`` logs which image env vars override the built-in defaults.

A persistent service pointed at a non-default image via an env var set once at
process startup is easy to forget about months later -- see
:func:`robovast.service.app.serve`.
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


_IMAGE_VARS = ("ROBOVAST_IMAGE", "ROBOVAST_ROQSIM_IMAGE",
               "ROBOVAST_CONTROLLER_IMAGE", "ROQSIM_IMAGE")


def _run_serve(monkeypatch):
    import uvicorn

    from robovast.service import app as app_module

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(app_module, "build_app", lambda impl, mount_mcp=True, auth_token=None: _StubApp())
    app_module.serve(impl=object())


def test_no_log_line_when_nothing_overridden(monkeypatch, caplog):
    for var in _IMAGE_VARS:
        monkeypatch.delenv(var, raising=False)
    with caplog.at_level(logging.INFO, logger="robovast.service.app"):
        _run_serve(monkeypatch)
    assert "image overrides" not in caplog.text


def test_logs_each_overridden_image_var(monkeypatch, caplog):
    monkeypatch.setenv("ROBOVAST_IMAGE", "docker.io/freeedlabs/robovast_jazzy@sha256:abc")
    monkeypatch.setenv("ROBOVAST_CONTROLLER_IMAGE",
                       "docker.io/freeedlabs/robovast-controller:latest")
    monkeypatch.delenv("ROBOVAST_ROQSIM_IMAGE", raising=False)
    monkeypatch.delenv("ROQSIM_IMAGE", raising=False)
    with caplog.at_level(logging.INFO, logger="robovast.service.app"):
        _run_serve(monkeypatch)
    assert ("image overrides from environment: "
            "ROBOVAST_IMAGE=docker.io/freeedlabs/robovast_jazzy@sha256:abc, "
            "ROBOVAST_CONTROLLER_IMAGE=docker.io/freeedlabs/robovast-controller:latest"
            in caplog.text)


def test_blank_env_value_is_not_treated_as_set(monkeypatch, caplog):
    for var in _IMAGE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ROQSIM_IMAGE", "   ")
    with caplog.at_level(logging.INFO, logger="robovast.service.app"):
        _run_serve(monkeypatch)
    assert "image overrides" not in caplog.text
