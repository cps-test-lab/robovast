# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Where ``vast serve`` looks for the built web UI, and why there are three places.

A wheel install used to ship no UI at all and say nothing about it. The resolver walked
*up* from ``__file__`` to a repo-root ``frontend/ui/dist`` — which, installed, points
above ``site-packages`` at whatever happens to be there. So the service silently served
API-only, and "I opened the URL and got JSON" was how you found out.

The fix has two halves and both have to hold: the assets are staged **inside** the package
(``make ui-stage``, then an explicit ``include`` because they are git-ignored), and the
resolver knows to look there. This pins the order, since it is the part that decides which
copy a developer sees when both exist.
"""

from pathlib import Path

import pytest

from robovast.service import app as app_mod


@pytest.fixture
def dist(tmp_path):
    """Build a fake dist directory; ``index.html`` is what the resolver tests for."""
    def _make(name: str) -> Path:
        d = tmp_path / name
        d.mkdir(parents=True)
        (d / "index.html").write_text("<!doctype html>")
        return d
    return _make


def test_the_env_override_wins(dist, monkeypatch):
    """How a container image points at assets baked in somewhere unrelated."""
    baked = dist("baked")
    monkeypatch.setenv("ROBOVAST_UI_DIST", str(baked))
    assert app_mod._ui_dist() == baked  # noqa: SLF001


def test_a_source_checkout_beats_a_staged_copy(monkeypatch, tmp_path):
    """`make ui-stage` leaves a copy inside the package, so a developer can have both.

    The live ``npm run build`` output is what they mean; a stale staged copy silently
    winning would be maddening to debug. So this builds the real checkout layout --
    ``<root>/src/robovast/service/app.py`` with both candidates in place -- and pins
    which one is chosen.
    """
    monkeypatch.delenv("ROBOVAST_UI_DIST", raising=False)
    module = tmp_path / "src" / "robovast" / "service" / "app.py"
    module.parent.mkdir(parents=True)
    source = tmp_path / "frontend" / "ui" / "dist"
    staged = tmp_path / "src" / "robovast" / "_ui"
    for d in (source, staged):
        d.mkdir(parents=True)
        (d / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr(app_mod, "__file__", str(module))

    assert app_mod._ui_dist() == source  # noqa: SLF001


def test_nothing_built_anywhere_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("ROBOVAST_UI_DIST", raising=False)
    monkeypatch.setattr(app_mod, "__file__", str(tmp_path / "pkg" / "service" / "app.py"))
    assert app_mod._ui_dist() is None  # noqa: SLF001


def test_the_packaged_location_is_inside_the_package(monkeypatch, tmp_path):
    """The bug in one assertion: an installed module cannot resolve *up* to the repo
    root, so the assets have to sit beside it — ``<package>/_ui``, not ``../../..``."""
    monkeypatch.delenv("ROBOVAST_UI_DIST", raising=False)
    pkg = tmp_path / "site-packages" / "robovast"
    (pkg / "service").mkdir(parents=True)
    (pkg / "_ui").mkdir()
    (pkg / "_ui" / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr(app_mod, "__file__", str(pkg / "service" / "app.py"))
    assert app_mod._ui_dist() == pkg / "_ui"  # noqa: SLF001


def test_a_missing_ui_warns_rather_than_passing_silently(monkeypatch, tmp_path, caplog):
    """Serving API-only is allowed; doing it quietly is how this went unnoticed."""
    monkeypatch.delenv("ROBOVAST_UI_DIST", raising=False)
    monkeypatch.setattr(app_mod, "__file__", str(tmp_path / "pkg" / "service" / "app.py"))

    class _App:
        def mount(self, *a, **k):
            raise AssertionError("nothing to mount")

    with caplog.at_level("WARNING"):
        app_mod._mount_ui(_App())  # noqa: SLF001
    assert any("web UI build not found" in r.message for r in caplog.records)
