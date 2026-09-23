# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A configuration drawn as a picture, from the contribution its config view draws.

Pins that every marker kind draws in every projection, that a file is drawn only by the
panel type declaring its role, and that whatever could not be drawn is said rather than
silently missing from the picture.
"""

import pytest

pytest.importorskip("matplotlib")

from robovast.common import config_plot  # noqa: E402  # pylint: disable=wrong-import-position

_PNG = b"\x89PNG"

_MARKERS = [
    {"kind": "path", "points": [[0, 0], [4, 0], [4, 3]], "label": "planned path"},
    {"kind": "pose", "pos": [0, 0], "yaw": 0.5, "label": "start"},
    {"kind": "point", "pos": [2, 1, 0.5]},
    {"kind": "box", "pos": [2, 2], "size": [1, 0.5, 1.2], "yaw": 0.3, "label": "obstacle"},
    {"kind": "cylinder", "pos": [3, 1], "radius": 0.2, "height": 1.0},
    {"kind": "sphere", "pos": [1, 3, 1.0], "radius": 0.3},
]


class _Panel:
    """A config panel type that draws role ``grid`` in the top-down view only."""

    FILE_ROLE = "grid"
    calls = []

    @staticmethod
    def plot(ax, path, projection, read):
        _Panel.calls.append((path, projection, read(path)))
        if projection != "xy":
            return False
        ax.axhline(0.0)
        return True


@pytest.fixture(autouse=True)
def _panels(monkeypatch):
    _Panel.calls = []
    monkeypatch.setattr(config_plot, "backgrounds", lambda: ({"grid": _Panel}, {}))


@pytest.mark.parametrize("projection", ["xy", "xz", "yz"])
def test_every_marker_kind_and_a_track_draw_in_every_projection(projection):
    png, notes = config_plot.draw(
        {"markers": _MARKERS, "files": {}}, lambda path: b"",
        track=[(0, 0, 0), (2, 0.1, 0), (4, 0.2, 0)], track_label="run 0", projection=projection)
    assert png.startswith(_PNG)
    assert notes == []


def test_a_file_is_drawn_by_the_panel_type_that_declares_its_role():
    _, notes = config_plot.draw({"markers": [], "files": {"grid": "_config/g.yaml"}},
                                lambda path: b"bytes of " + path.encode())
    assert _Panel.calls == [("_config/g.yaml", "xy", b"bytes of _config/g.yaml")]
    assert notes == []


def test_what_cannot_be_drawn_is_said_not_silently_missing():
    _, notes = config_plot.draw(
        {"markers": [], "files": {"grid": "_config/g.yaml", "mesh": "_config/w.stl"},
         "errors": ["ObstacleVariation: boom"]},
        lambda path: b"", projection="xz")
    assert any("'mesh'" in n and "no installed panel type" in n for n in notes)
    assert any("'grid'" in n and "xz" in n for n in notes)
    assert any("ObstacleVariation: boom" in n for n in notes)


def test_an_unknown_projection_is_refused():
    with pytest.raises(ValueError, match="xy"):
        config_plot.draw({"markers": []}, lambda path: b"", projection="top")


def test_a_panel_type_that_did_not_load_is_named_on_the_picture(monkeypatch):
    """"Nothing installed draws this" and "what draws it did not load" are different facts,
    and the second reads as the first on a picture that leaves the background out."""
    monkeypatch.setattr(config_plot, "backgrounds",
                        lambda: ({}, {"map2d": "No module named 'matplotlib'"}))

    _png, notes = config_plot.draw(
        {"markers": [], "files": {}}, read=lambda path: b"", projection="xy")

    assert any("map2d" in note and "did not load" in note for note in notes)
