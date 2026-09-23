"""The run view's costmap panel, drawn onto the frames of a rendered video.

``roqsim render`` draws a recording one sample per frame and lets an installed package paint on
each frame (its ``roqsim.render_overlays`` entry-point group). This is the navigation package's painter:
the same picture the web run view's ``costmap`` panel shows -- the static map, the global and local
costmaps nearest the frame's time, the driven trail up to it, the robot, and what the config
view draws for the configuration -- the planned path, goal and obstacles its variations contributed,
and the markers the campaign's ``map2d`` panel declares -- as an inset in a corner of the video, in
step with the simulation because both sides carry simulated seconds.

It reads the files postprocessing wrote beside the recording rather than the service, so a run
fetched to disk is enough::

    <campaign>/_transient/configurations.yaml     the planned path, goal and obstacles
    <campaign>/_config/<campaign>.vast            the map2d panel's declared markers
    <campaign>/<config>/<run>/costmaps.csv        rosbags_costmap_to_csv: the grids, by topic
    <campaign>/<config>/<run>/poses.csv           rosbags_tf_to_csv: the robot, and any frame a grid is in
    <campaign>/<config>/<run>/run.npz             the recording being drawn

The options mirror the ``.vast`` panel binding, so a layer set that works in the web UI works
here::

    roqsim render --state run.npz --overlay costmap --out clip.mp4
    roqsim render --state run.npz --overlay '{"costmap": {"anchor": "top-right", "width": 0.3,
        "layers": {"map": {"topic": "/map"}, "local": {"topic": "/local_costmap/costmap"}}}}'

``markers`` takes the same declarations the ``map2d`` panel does (``{kind: pose, pos: [x, y]}``,
``{kind: pose, param: goal_pose}``), resolved against the configuration the same way. Unstated,
the campaign's own ``map2d`` declaration is drawn beside the contributed markers, and goes with
them when all three of ``planned_path``/``goal``/``obstacles`` are off.

Nothing here imports roqsim: the overlay is found by name through the entry point and speaks the
small duck-typed contract roqsim documents (``prepare(width, height, *, state)``, ``draw(frame,
t)``). What it cannot find, it refuses by file name -- a layer silently left blank would read as
"nav2 saw nothing here", which is a different claim.
"""

from __future__ import annotations

import base64
import bisect
import csv
import logging
import math
import statistics
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import yaml
from PIL import Image, ImageDraw

from robovast.common.panel_bindings import declared_markers
from robovast.common.scene_markers import SceneMarker

from .config_view import GOAL_COLOR, obstacle_markers, path_markers

log = logging.getLogger(__name__)

#: The layers the web panel draws by default, in its vocabulary. With no binding stated, the ones
#: the run recorded are taken (see ``CostmapOverlay._recorded_defaults``).
DEFAULT_LAYERS = {
    "map": {"topic": "/map"},
    "global": {"topic": "/global_costmap/costmap"},
    "local": {"topic": "/local_costmap/costmap"},
    "poses": {"table": "poses"},
}

#: The web panel's palette (``web/src/occupancyGrid.ts``, ``costmap.tsx``), kept identical so the
#: inset and the panel read as one picture of the same data.
BACKGROUND = (0x12, 0x17, 0x1F, 255)
TRAIL_COLOR = "#2dd4bf"
ROBOT_COLOR = "#f0b429"
GLOBAL_ALPHA = 110
LOCAL_ALPHA = 210

#: A grid farther from the cursor than this many of its own publish periods is withheld and named,
#: as the web panel withholds it: drawn as current, it would put the local window where the robot
#: no longer is.
STALE_PERIODS = 2

OPTIONS = frozenset({
    "run", "layers", "robot_frame", "trail", "planned_path", "obstacles", "goal", "markers",
    "stale_after",
})


class NavVideoError(ValueError):
    """The overlay cannot draw what it was asked to (see the message)."""


# -- the palette, as lookup tables over a cell's raw byte ----------------------------------------


def map_color(v: int) -> tuple[int, int, int, int]:
    """rviz "map" grayscale: unknown (-1) transparent, free (0) light, occupied (100) dark."""
    if v < 0:
        return (0, 0, 0, 0)
    shade = round(255 * (1 - min(v, 100) / 100))
    return (shade, shade, shade, 255)


def costmap_color(v: int, alpha: int = 150) -> tuple[int, int, int, int]:
    """rviz "costmap" gradient: free/unknown transparent, 1..98 blue->red, 99 cyan, 100 purple."""
    if v <= 0:
        return (0, 0, 0, 0)
    c = min(v, 100)
    if c >= 100:
        return (255, 0, 255, alpha)
    if c >= 99:
        return (0, 255, 255, alpha)
    r = round(c * 255 / 100)
    return (r, 0, 255 - r, alpha)


def palette(color) -> np.ndarray:
    """``(256, 4)`` uint8: the colour of every byte a cell can hold (int8 read as uint8)."""
    lut = np.zeros((256, 4), dtype=np.uint8)
    for i in range(256):
        lut[i] = color(i if i < 128 else i - 256)
    return lut


def layer_palette(name: str) -> np.ndarray:
    if name == "map":
        return palette(map_color)
    alpha = GLOBAL_ALPHA if name == "global" else LOCAL_ALPHA
    return palette(lambda v: costmap_color(v, alpha))


# -- what the run directory holds ------------------------------------------------------------------


@dataclass
class GridRow:
    t: float
    frame_id: str
    resolution: float
    width: int
    height: int
    origin_x: float
    origin_y: float
    origin_yaw: float
    data: str  # base64 of zlib of int8 cells, row-major, row 0 at the grid's origin

    def cells(self) -> np.ndarray:
        raw = zlib.decompress(base64.b64decode(self.data))
        cells = np.frombuffer(raw, dtype=np.int8)
        if cells.size != self.width * self.height:
            raise NavVideoError(
                f"costmap frame at t={self.t:.3f} s holds {cells.size} cells for a "
                f"{self.width}x{self.height} grid"
            )
        return cells.reshape(self.height, self.width)


@dataclass
class GridTopic:
    """Every recorded frame of one topic, by time, and how far apart they were published."""

    topic: str
    rows: list[GridRow]
    times: list[float]
    period: Optional[float]

    def nearest(self, t: float) -> int:
        i = bisect.bisect_left(self.times, t)
        if i <= 0:
            return 0
        if i >= len(self.times):
            return len(self.times) - 1
        return i if self.times[i] - t < t - self.times[i - 1] else i - 1


@dataclass
class PoseTrack:
    times: np.ndarray
    x: np.ndarray
    y: np.ndarray
    yaw: np.ndarray

    def nearest(self, t: float) -> int:
        i = int(np.searchsorted(self.times, t))
        if i <= 0:
            return 0
        if i >= len(self.times):
            return len(self.times) - 1
        return i if self.times[i] - t < t - self.times[i - 1] else i - 1


def read_costmaps(path: Path) -> dict[str, GridTopic]:
    """``costmaps.csv`` by topic. Payloads exceed csv's default field limit, so it is raised."""
    csv.field_size_limit(sys.maxsize)
    by_topic: dict[str, list[GridRow]] = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            by_topic.setdefault(row["topic"], []).append(
                GridRow(
                    t=float(row["timestamp"]),
                    frame_id=row.get("frame_id") or "",
                    resolution=float(row["resolution"]),
                    width=int(row["width"]),
                    height=int(row["height"]),
                    origin_x=float(row["origin_x"]),
                    origin_y=float(row["origin_y"]),
                    origin_yaw=float(row.get("origin_yaw") or 0.0),
                    data=row["data"],
                )
            )
    out = {}
    for topic, rows in by_topic.items():
        rows.sort(key=lambda r: r.t)
        times = [r.t for r in rows]
        gaps = [b - a for a, b in zip(times, times[1:], strict=False) if b > a]
        out[topic] = GridTopic(topic, rows, times, statistics.median(gaps) if gaps else None)
    return out


def _yaw(qx: float, qy: float, qz: float, qw: float) -> float:
    return math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))


def read_poses(path: Path) -> dict[str, PoseTrack]:
    """``poses.csv`` by frame: time, position and yaw, in the map frame, sorted by time."""
    tracks: dict[str, list[tuple[float, float, float, float]]] = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                tracks.setdefault(row["frame"], []).append(
                    (
                        float(row["timestamp"]),
                        float(row["position.x"]),
                        float(row["position.y"]),
                        _yaw(
                            float(row["orientation.x"]),
                            float(row["orientation.y"]),
                            float(row["orientation.z"]),
                            float(row["orientation.w"]),
                        ),
                    )
                )
            except (KeyError, ValueError) as err:
                raise NavVideoError(f"{path}: a pose row is not readable: {err}") from None
    out = {}
    for frame, rows in tracks.items():
        rows.sort()
        arr = np.array(rows, dtype=float).reshape(-1, 4)
        out[frame] = PoseTrack(arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3])
    return out


def configurations(campaign_dir: Path) -> dict:
    """The campaign's ``_transient/configurations.yaml``."""
    path = campaign_dir / "_transient" / "configurations.yaml"
    if not path.is_file():
        raise NavVideoError(
            f"{path} is missing: the planned path, goal and obstacles are read from it. Fetch it "
            "with the run, or pass planned_path/goal/obstacles: false to draw without them."
        )
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def configuration_entry(campaign_dir: Path, config_name: str) -> dict:
    """The configuration's entry in the campaign's ``_transient/configurations.yaml``."""
    doc = configurations(campaign_dir)
    path = campaign_dir / "_transient" / "configurations.yaml"
    for entry in doc.get("configs") or []:
        if entry.get("name") == config_name:
            return entry
    have = ", ".join(e.get("name", "?") for e in doc.get("configs") or []) or "none"
    raise NavVideoError(
        f"{path} has no configuration named {config_name!r} (it has: {have}); the run directory "
        "is expected to be <campaign>/<config>/<run>/."
    )


def map2d_declaration(campaign_dir: Path) -> dict:
    """The ``map2d`` config-view panel's bindings, from the campaign's frozen ``.vast``.

    ``configurations.yaml`` names the file the campaign ran; the campaign keeps a copy under
    ``_config/``. The map2d panel is the one whose markers are in the map frame -- the frame this
    inset is -- so its declaration is what the inset draws; ``scene3d``'s are world-frame and
    carry offsets a map cannot undo.
    """
    named = configurations(campaign_dir).get("vast")
    if not named:
        return {}
    path = campaign_dir / "_config" / Path(str(named)).name
    if not path.is_file():
        raise NavVideoError(
            f"{path} is missing: the markers the campaign declares on its map2d panel are read "
            "from it. Fetch it with the run, or pass markers: [] to draw without them."
        )
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    panels = ((doc.get("visualization") or {}).get("config") or {}).get("panels") or []
    merged: dict = {"markers": []}
    for panel in panels:
        if isinstance(panel, dict) and isinstance(panel.get("map2d"), dict):
            merged["markers"] += list(panel["map2d"].get("markers") or [])
    return merged


def map_from_file(path: Path) -> GridRow:
    """A ROS map YAML as one grid row: free where the image is white, occupied where dark.

    A file is a coarser source than the ``/map`` topic (the thresholds are the map server's, and
    only its verdict is recorded on the topic), so the topic is the default and this the option.
    """
    from .map_loader import load_map

    m = load_map(str(path))
    pixels = np.asarray(m.map_array)
    cells = np.full(pixels.shape, -1, dtype=np.int8)
    cells[pixels >= 254] = 0
    cells[pixels < 100] = 100
    cells = cells[::-1]  # image row 0 is the top; a grid's row 0 is its origin (bottom)
    data = base64.b64encode(zlib.compress(cells.tobytes(), 9)).decode("ascii")
    return GridRow(
        t=0.0,
        frame_id="map",
        resolution=float(m.resolution),
        width=int(m.width),
        height=int(m.height),
        origin_x=float(m.origin_x),
        origin_y=float(m.origin_y),
        origin_yaw=float(m.origin_theta),
        data=data,
    )


# -- geometry ---------------------------------------------------------------------------------------


def _rot(yaw: float) -> np.ndarray:
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _shift(x: float, y: float) -> np.ndarray:
    return np.array([[1.0, 0.0, x], [0.0, 1.0, y], [0.0, 0.0, 1.0]])


def _scale(sx: float, sy: float) -> np.ndarray:
    return np.array([[sx, 0.0, 0.0], [0.0, sy, 0.0], [0.0, 0.0, 1.0]])


def grid_extent(row: GridRow, frame: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` of a grid in the map frame, through the frame's pose."""
    to_map = _shift(frame[0], frame[1]) @ _rot(frame[2]) @ _shift(row.origin_x, row.origin_y) @ _rot(
        row.origin_yaw
    )
    w, h = row.width * row.resolution, row.height * row.resolution
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], dtype=float) @ to_map.T
    return (
        float(corners[:, 0].min()),
        float(corners[:, 1].min()),
        float(corners[:, 0].max()),
        float(corners[:, 1].max()),
    )


# -- the overlay -----------------------------------------------------------------------------------


class CostmapOverlay:
    """The run view's costmap panel as an inset on every frame of a video."""

    name = "costmap"

    def __init__(
        self,
        placement,
        *,
        run: str | Path | None = None,
        layers: dict | None = None,
        robot_frame: str = "base_link",
        trail: bool = True,
        planned_path: bool = True,
        obstacles: bool = True,
        goal: bool = True,
        markers: list | None = None,
        stale_after: float | None = None,
    ) -> None:
        self.placement = placement
        self.run = Path(run) if run else None
        #: ``None`` until prepare(): with no binding stated, the panel's default layers are taken
        #: as far as the run recorded them, and which ones that was is logged.
        self.layers = dict(layers) if layers is not None else None
        self.robot_frame = str(robot_frame)
        self.trail, self.planned_path, self.obstacles, self.goal = trail, planned_path, obstacles, goal
        #: ``None`` until prepare(): unstated, the campaign's map2d declaration is drawn.
        self.markers = list(markers) if markers is not None else None
        self.stale_after = None if stale_after is None else float(stale_after)
        self._grids: dict[str, GridTopic] = {}
        self._static: dict[str, GridRow] = {}
        self._poses: dict[str, PoseTrack] = {}
        self._markers: list = []
        self._palettes: dict[str, np.ndarray] = {}
        self._decoded: dict[str, tuple[int, Image.Image]] = {}
        self._panel: tuple[int, int] | None = None
        self._view: tuple[float, float, float] | None = None  # cx, cy, pixels per metre

    @classmethod
    def from_spec(cls, options: dict, placement) -> CostmapOverlay:
        if unknown := set(options) - OPTIONS:
            raise NavVideoError(
                f"overlay 'costmap': unknown option(s) {', '.join(sorted(unknown))}; it takes "
                f"{', '.join(sorted(OPTIONS))} and the placement keys"
            )
        return cls(placement, **options)

    # -- loading -----------------------------------------------------------------------------------

    def prepare(self, width: int, height: int, *, state=None) -> None:
        run = self.run or (Path(state).parent if state else None)
        if run is None:
            raise NavVideoError(
                "overlay 'costmap': no run directory. Pass run: <dir>, or render a recording that "
                "sits in one."
            )
        if not run.is_dir():
            raise NavVideoError(f"overlay 'costmap': {run} is not a directory")
        self.run = run
        if self.layers is None:
            self.layers = self._recorded_defaults(run / "costmaps.csv")

        poses_table = (self.layers.get("poses") or {}).get("table", "poses")
        poses_path = run / f"{poses_table}.csv"
        if not poses_path.is_file():
            raise NavVideoError(
                f"{poses_path} is missing: the robot's trail and pose come from it. It is written by "
                "rosbags_tf_to_csv; its frames must include the robot's and any frame a costmap is in."
            )
        self._poses = read_poses(poses_path)
        if self.robot_frame not in self._poses:
            raise NavVideoError(
                f"{poses_path} has no frame {self.robot_frame!r} (it has: "
                f"{', '.join(sorted(self._poses)) or 'none'}); pass robot_frame: <frame>."
            )

        grid_layers = {n: b for n, b in self.layers.items() if n != "poses"}
        topics = {n: b["topic"] for n, b in grid_layers.items() if isinstance(b, dict) and b.get("topic")}
        files = {n: b["file"] for n, b in grid_layers.items() if isinstance(b, dict) and b.get("file")}
        for name, binding in grid_layers.items():
            if name not in topics and name not in files:
                raise NavVideoError(
                    f"overlay 'costmap': layer {name!r} names neither a topic nor a file: {binding!r}"
                )
        if topics:
            costmaps_path = run / "costmaps.csv"
            if not costmaps_path.is_file():
                raise NavVideoError(
                    f"{costmaps_path} is missing: the {', '.join(sorted(topics))} layer(s) read their "
                    "grids from it. It is written by rosbags_costmap_to_csv, which must list "
                    f"{', '.join(sorted(topics.values()))}."
                )
            recorded = read_costmaps(costmaps_path)
            for name, topic in topics.items():
                if topic not in recorded:
                    raise NavVideoError(
                        f"{costmaps_path} holds no frames of {topic} (layer {name!r}); it has: "
                        f"{', '.join(sorted(recorded)) or 'none'}. Add the topic to "
                        "rosbags_costmap_to_csv, or drop the layer."
                    )
                self._grids[name] = recorded[topic]
        for name, file in files.items():
            campaign_dir = run.parent.parent
            path = Path(file) if Path(file).is_absolute() else campaign_dir / file
            if not path.is_file():
                raise NavVideoError(f"overlay 'costmap': layer {name!r}: no map at {path}")
            self._static[name] = map_from_file(path)
        for name in grid_layers:
            self._palettes[name] = layer_palette(name)

        # A grid in another frame (the local costmap is in `odom`) needs that frame's poses.
        needed = {r.frame_id for g in self._grids.values() for r in g.rows} - {"map", ""}
        if missing := needed - set(self._poses):
            raise NavVideoError(
                f"costmap frames are in {', '.join(sorted(missing))}, which {poses_path} does not "
                f"carry (it has: {', '.join(sorted(self._poses))}). Add the frame to "
                "rosbags_tf_to_csv's frames."
            )

        if self.planned_path or self.obstacles or self.goal or self.markers:
            campaign = run.parent.parent
            entry = configuration_entry(campaign, run.parent.name)
            markers = []
            if self.planned_path or self.goal:
                for m in path_markers(entry):
                    if m.kind == "path" and not self.planned_path:
                        continue
                    if m.kind == "pose" and m.color == GOAL_COLOR and not self.goal:
                        continue
                    markers.append(m)
            if self.obstacles:
                markers += obstacle_markers(entry)
            declaration = {"markers": self.markers} if self.markers is not None else map2d_declaration(campaign)
            markers += [SceneMarker(**m) for m in declared_markers(declaration, entry)]
            self._markers = markers

        self._fit(width, height)

    @staticmethod
    def _recorded_defaults(costmaps_path: Path) -> dict:
        """The panel's default layers, as far as this run recorded them.

        A stated binding is held to the letter; an unstated one means "what the panel would show",
        and a campaign that recorded only its global costmap still gets a picture rather than a
        refusal over the ``/map`` it never had. Which layers were taken is logged, so the choice is
        visible; a run with none of the three is refused naming what it does have.
        """
        if not costmaps_path.is_file():
            raise NavVideoError(
                f"{costmaps_path} is missing: the costmap layers read their grids from it. It is "
                "written by rosbags_costmap_to_csv, which must list the costmap topics."
            )
        recorded = read_costmaps(costmaps_path)
        layers = {
            name: binding
            for name, binding in DEFAULT_LAYERS.items()
            if name == "poses" or binding.get("topic") in recorded
        }
        if len(layers) == 1:
            wanted = ", ".join(b["topic"] for n, b in DEFAULT_LAYERS.items() if n != "poses")
            raise NavVideoError(
                f"{costmaps_path} holds none of {wanted}; it has: "
                f"{', '.join(sorted(recorded)) or 'none'}. Bind a layer to one of those with "
                "layers: {<name>: {topic: ...}}, or add the topics to rosbags_costmap_to_csv."
            )
        log.info(
            "costmap overlay: layers %s (the panel's defaults this run recorded)",
            ", ".join(n for n in layers if n != "poses"),
        )
        return layers

    def _fit(self, frame_w: int, frame_h: int) -> None:
        """The inset's size and its metres-to-pixels, from the map layer's extent."""
        extent = None
        for name in ("map", "global"):
            if name in self._static:
                extent = grid_extent(self._static[name], (0.0, 0.0, 0.0))
                break
            if name in self._grids:
                row = self._grids[name].rows[0]
                extent = grid_extent(row, self._frame_pose(row.frame_id, row.t))
                break
        if extent is None:
            xs, ys = self._poses[self.robot_frame].x, self._poses[self.robot_frame].y
            extent = (float(xs.min()) - 2, float(ys.min()) - 2, float(xs.max()) + 2, float(ys.max()) + 2)
        ew = max(extent[2] - extent[0], 0.1)
        eh = max(extent[3] - extent[1], 0.1)
        panel_w = max(32, self.placement.pixels(frame_w))
        panel_h = max(32, min(int(round(panel_w * eh / ew)), int(frame_h * 0.9)))
        ppm = min(panel_w / (ew * 1.05), panel_h / (eh * 1.05))
        self._panel = (panel_w, panel_h)
        self._view = ((extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2, ppm)

    # -- drawing -------------------------------------------------------------------------------------

    def _frame_pose(self, frame_id: str, t: float) -> tuple[float, float, float]:
        if not frame_id or frame_id == "map":
            return (0.0, 0.0, 0.0)
        track = self._poses[frame_id]
        i = track.nearest(t)
        return (float(track.x[i]), float(track.y[i]), float(track.yaw[i]))

    def _to_pixels(self, x: float, y: float) -> tuple[float, float]:
        cx, cy, ppm = self._view
        w, h = self._panel
        return w / 2 + (x - cx) * ppm, h / 2 - (y - cy) * ppm

    def _decoded_image(self, name: str, index: int, row: GridRow) -> Image.Image:
        """The grid as an RGBA image, row 0 at the TOP (max y); cached per topic and row."""
        cached = self._decoded.get(name)
        if cached is not None and cached[0] == index:
            return cached[1]
        rgba = self._palettes[name][row.cells().view(np.uint8)]
        image = Image.fromarray(np.ascontiguousarray(rgba[::-1]), "RGBA")
        self._decoded[name] = (index, image)
        return image

    def _place_grid(self, canvas: Image.Image, image: Image.Image, row: GridRow, frame) -> None:
        """Composite a grid image through its frame's pose and the grid's origin onto the canvas.

        One affine map from a canvas pixel back to a grid pixel, so a rotated frame (odom under a
        drifted robot) and a rotated origin cost nothing more than an axis-aligned one.
        """
        cx, cy, ppm = self._view
        w, h = self._panel
        # canvas pixel (u, v) -> map metres -> frame metres -> grid metres -> grid image pixel
        px_to_map = _shift(cx, cy) @ _scale(1 / ppm, -1 / ppm) @ _shift(-w / 2, -h / 2)
        map_to_grid = _rot(-row.origin_yaw) @ _shift(-row.origin_x, -row.origin_y) @ _rot(-frame[2]) @ _shift(-frame[0], -frame[1])
        grid_to_img = _shift(0.0, row.height) @ _scale(1 / row.resolution, -1 / row.resolution)
        m = grid_to_img @ map_to_grid @ px_to_map
        warped = image.transform(
            (w, h), Image.Transform.AFFINE, tuple(m[:2].ravel()), resample=Image.Resampling.NEAREST
        )
        canvas.alpha_composite(warped)

    def render(self, t: float) -> Image.Image:
        """The panel at simulated time ``t``, as an RGBA image of the inset's size."""
        if self._panel is None:
            raise NavVideoError("overlay 'costmap': prepare() was not called")
        w, h = self._panel
        canvas = Image.new("RGBA", (w, h), BACKGROUND)
        withheld: list[str] = []

        def draw_layer(name: str) -> None:
            if name in self._static:
                row = self._static[name]
                self._place_grid(canvas, self._decoded_image(name, 0, row), row, (0.0, 0.0, 0.0))
                return
            topic = self._grids.get(name)
            if topic is None:
                return
            index = topic.nearest(t)
            row = topic.rows[index]
            age = abs(t - row.t)
            limit = self.stale_after
            if limit is None and topic.period:
                limit = STALE_PERIODS * topic.period
            if limit is not None and age > limit:
                withheld.append(f"{name}: nearest frame {age:.1f} s away")
                return
            # Composed at the frame's OWN time: a local costmap's origin is expressed in `odom` as
            # of when it was published, and resolving odom->map at the cursor instead would place
            # every obstacle cell at a slightly wrong map coordinate.
            frame = self._frame_pose(row.frame_id, row.t)
            self._place_grid(canvas, self._decoded_image(name, index, row), row, frame)

        # map first, then global, then everything else (the local window sits on top).
        order = ["map", "global"] + [n for n in self._palettes if n not in ("map", "global")]
        for name in order:
            draw_layer(name)

        draw = ImageDraw.Draw(canvas)
        self._draw_markers(draw)
        track = self._poses[self.robot_frame]
        if self.trail:
            upto = int(np.searchsorted(track.times, t, side="right"))
            if upto >= 2:
                points = [self._to_pixels(x, y) for x, y in zip(track.x[:upto], track.y[:upto], strict=True)]
                draw.line(points, fill=TRAIL_COLOR, width=2)
        i = track.nearest(t)
        self._draw_robot(draw, float(track.x[i]), float(track.y[i]), float(track.yaw[i]))
        if withheld:
            draw.text((6, h - 14 * len(withheld) - 4), "\n".join(withheld), fill=(255, 210, 120, 255))
        return canvas

    def _draw_markers(self, draw: ImageDraw.ImageDraw) -> None:
        ppm = self._view[2]
        for m in self._markers:
            color = m.color or "#ffffff"
            if m.kind == "path" and m.points:
                pts = [self._to_pixels(p[0], p[1]) for p in m.points]
                if len(pts) >= 2:
                    draw.line(pts, fill=color, width=2)
            elif m.kind == "pose" and m.pos:
                x, y = self._to_pixels(m.pos[0], m.pos[1])
                r = 4
                draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
                if m.yaw is not None:
                    draw.line((x, y, x + 10 * math.cos(m.yaw), y - 10 * math.sin(m.yaw)), fill=color, width=2)
                if m.label:
                    draw.text((x + r + 2, y - r - 8), m.label, fill=color)
            elif m.kind == "box" and m.pos and m.size:
                hx, hy, yaw = m.size[0] / 2, m.size[1] / 2, m.yaw or 0.0
                c, s = math.cos(yaw), math.sin(yaw)
                corners = [
                    self._to_pixels(m.pos[0] + c * dx - s * dy, m.pos[1] + s * dx + c * dy)
                    for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
                ]
                draw.polygon(corners, outline=color, fill=_translucent(color))
            elif m.kind in ("cylinder", "sphere") and m.pos and m.radius:
                x, y = self._to_pixels(m.pos[0], m.pos[1])
                r = m.radius * ppm
                draw.ellipse((x - r, y - r, x + r, y + r), outline=color, fill=_translucent(color))
            elif m.kind == "point" and m.pos:
                x, y = self._to_pixels(m.pos[0], m.pos[1])
                draw.ellipse((x - 1.5, y - 1.5, x + 1.5, y + 1.5), fill=color)

    def _draw_robot(self, draw: ImageDraw.ImageDraw, x: float, y: float, yaw: float) -> None:
        sx, sy = self._to_pixels(x, y)
        dx, dy = math.cos(yaw), -math.sin(yaw)
        nx, ny = -dy, dx
        length, width = 12.0, 6.0
        draw.polygon(
            [
                (sx + dx * length, sy + dy * length),
                (sx - dx * length * 0.6 + nx * width, sy - dy * length * 0.6 + ny * width),
                (sx - dx * length * 0.6 - nx * width, sy - dy * length * 0.6 - ny * width),
            ],
            fill=ROBOT_COLOR,
            outline=(0, 0, 0, 150),
        )

    def draw(self, frame: np.ndarray, t: float) -> np.ndarray:
        """Paint the panel at ``t`` onto ``frame`` (HxWx3 uint8) at its placement, in place."""
        panel = self.render(t)
        h, w = frame.shape[:2]
        x, y = self.placement.box(w, h, panel.width, panel.height)
        canvas = Image.fromarray(frame)
        canvas.paste(panel, (int(x), int(y)), panel)
        frame[...] = np.asarray(canvas)
        return frame


def _translucent(color: str, alpha: int = 70) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


__all__ = [
    "CostmapOverlay",
    "NavVideoError",
    "DEFAULT_LAYERS",
    "map_color",
    "costmap_color",
    "palette",
    "read_costmaps",
    "read_poses",
    "grid_extent",
]
