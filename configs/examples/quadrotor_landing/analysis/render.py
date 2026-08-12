# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Quadrotor descent-animation postprocessing plugin.

Per *run*, renders the raw ``trajectory.csv`` the sim writes into an animated
``descent.gif`` beside it: the craft's x-z path with its tilt, against the landing
pad. ``analysis_run.ipynb`` embeds that file, so the animation is produced once
here rather than re-rendered on every notebook open.

Used as a local-file postprocessing plugin
(``./analysis/render.py:DescentVideo``) in ``results_processing.postprocessing``.
Run discovery is depth-agnostic (globs for ``trajectory.csv``), matching
``search/metrics.py:QuadMetrics`` -- so it works pointed at the parent results dir
or at a single campaign root.
"""

import csv
from pathlib import Path
from typing import Tuple

from robovast.results_processing.postprocessing_plugins import \
    BasePostprocessingPlugin

#: Matches ``quadrotor_sim.py``'s ``--pad-radius`` default; drawn for scale only.
PAD_RADIUS = 0.5

#: Frames per second of the rendered GIF. The sim's own step is much finer, so
#: frames are subsampled -- and the figure kept small -- because the run notebook
#: embeds this file as a base64 data: URI, which inflates it by a third again.
FPS = 20
MAX_FRAMES = 80
DPI = 72


def _read_trajectory(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return [{k: float(v) for k, v in row.items()} for row in csv.DictReader(f)]


def _render_gif(rows: list[dict], out: Path) -> None:
    # Imported here, not at module import: this plugin is loaded by reference
    # whenever the .vast is validated, and a missing matplotlib should fail the
    # step that renders -- with a message naming it -- not every check of the file.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, PillowWriter

    step = max(1, len(rows) // MAX_FRAMES)
    frames = rows[::step]

    xs = [r["x"] for r in rows]
    zs = [r["z"] for r in rows]
    margin = 0.5
    fig, ax = plt.subplots(figsize=(4, 4))
    ax.set_xlim(min(min(xs), -PAD_RADIUS) - margin, max(max(xs), PAD_RADIUS) + margin)
    ax.set_ylim(min(0.0, min(zs)) - 0.1, max(zs) + margin)
    ax.set_xlabel("x [m]")
    ax.set_ylabel("z [m]")
    ax.plot([-PAD_RADIUS, PAD_RADIUS], [0, 0], "g-", lw=4)
    trail, = ax.plot([], [], "c-", lw=1, alpha=0.6)
    # A short line segment standing in for the airframe, so its tilt is visible.
    craft, = ax.plot([], [], "k-", lw=3)

    def update(i):
        frame = frames[i]
        trail.set_data([r["x"] for r in frames[:i + 1]], [r["z"] for r in frames[:i + 1]])
        # Rotate a half-metre body about its centre by the recorded tilt.
        half = 0.25
        import math
        dx, dz = half * math.cos(frame["tilt"]), half * math.sin(frame["tilt"])
        craft.set_data([frame["x"] - dx, frame["x"] + dx],
                       [frame["z"] - dz, frame["z"] + dz])
        return trail, craft

    anim = FuncAnimation(fig, update, frames=len(frames), blit=True)
    anim.save(out, writer=PillowWriter(fps=FPS), dpi=DPI)
    plt.close(fig)


class DescentVideo(BasePostprocessingPlugin):
    def __call__(self, results_dir: str, config_dir: str,
                 trajectory: str = "trajectory.csv", file: str = "descent.gif",
                 force: bool = False, **kwargs) -> Tuple[bool, str]:
        written = skipped = 0
        for traj in sorted(Path(results_dir).rglob(trajectory)):
            out = traj.parent / file
            # Incremental, like QuadMetrics: an output at least as new as its input is
            # already current, so re-running over a whole campaign only renders new
            # runs. Rendering is the expensive step here, so this matters more.
            if not force and out.exists() and out.stat().st_mtime >= traj.stat().st_mtime:
                skipped += 1
                continue
            rows = _read_trajectory(traj)
            if not rows:
                continue
            _render_gif(rows, out)
            written += 1
        suffix = f" ({skipped} up-to-date)" if skipped else ""
        return True, f"DescentVideo wrote {file} for {written} run(s){suffix}"
