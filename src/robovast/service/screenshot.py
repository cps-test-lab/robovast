# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Re-rendering one moment of a run, from a viewpoint the caller picks.

The sibling of :mod:`robovast.service.scene_cache`, and deliberately the *small* one. Both run
a command in the campaign's own pinned simulator image through one ``shell`` input-generator
entry; what they differ in is what that buys:

**Geometry is cached, a screenshot is not.** A scene descriptor is keyed on the world, so one
build serves every run and every campaign that used it — worth a background thread, a cache
directory, an eviction policy and a status a viewer polls. A screenshot is keyed on a camera
pose and a moment, so the key space is unbounded and every call is a run. Caching it would
grow a directory nobody ever hits twice.

**A render is kept, though, under a name of its own.** Not so a second request can reuse it —
none would — but so the one that asked for it can pass it on: an image that exists only inline
in a reply cannot be attached, saved or shown to someone else. :func:`keep` moves each render
into :func:`kept_root` under a fresh name, which the service serves at
``Routes.campaign_screenshot_frame``. The store is bounded rather than cached: a render is kept
for :data:`KEEP_S` after it was made, and at most :data:`KEEP_MAX` renders are kept, the oldest
removed first.

**So this is synchronous, and that is the point.** An asynchronous render would have to stash
its failure reason somewhere the caller could find after the request returned — which is
exactly the in-memory dictionary that made a failed scene build invisible to everything but a
browser. A synchronous call returns its error in the response and has nothing to record.

The cost is honest and worth stating where callers see it: the image has to be on the node. A
warm image renders in seconds; a cold one pays for the pull first, inside the request.
"""

import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Written by the backend's command into the entry's output directory.
FRAME_NAME = "frame.png"

#: How long a kept render stays addressable after it was made.
KEEP_S = 24 * 3600

#: How many renders are kept at most, across every campaign; the oldest go first.
KEEP_MAX = 200

#: What a kept render is named: a fresh random hex id. A name is matched in full before it
#: reaches the filesystem, so a request cannot address anything outside the store.
_KEPT_NAME = re.compile(r"[0-9a-f]{32}\.png")

#: A campaign id as a directory of the store: one path segment, never ``.`` or ``..``.
_KEPT_CAMPAIGN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")

_prune_lock = threading.Lock()

#: Where a campaign-file world is mounted back, so the world resolves its own references
#: exactly as it did during the run. The same path :mod:`scene_cache` uses, for the same reason.
_RUN_FILE_MOUNT = "/config"


class ScreenshotUnavailable(ValueError):
    """This run cannot be rendered, with the reason a caller can act on.

    Separate from ``SceneUnavailable`` because the answers differ: geometry can be missing
    because it has not been built *yet*, while a screenshot is either possible now or not at
    all. A ``ValueError`` so the HTTP layer's ``_guard`` turns it into a **400 carrying the
    reason** rather than a 500 with a traceback — every case here is the request asking for
    something this campaign cannot give (a simulator that does not render, a run that recorded
    no state, a camera combined with a view), which is a statement about the request.
    """


def state_filename(identity: dict) -> str:
    """The recording this campaign's simulator writes per run, or refuse saying it writes none.

    Asked of the backend rather than assumed, for the same reason the render command is: what a
    run records is the simulator's decision, and a constant here would be a second place to
    change when a backend's answer does.
    """
    from robovast.common.simulators import \
        run_state_filename  # pylint: disable=import-outside-toplevel

    filename = run_state_filename(identity.get("execution") or {})
    if not filename:
        raise ScreenshotUnavailable(
            f"this campaign's simulator ({identity.get('backend') or 'none configured'}) "
            "records no run state, so there is no moment to re-render.")
    return filename


def check_camera_args(view: dict, focus: list, camera: Optional[str]) -> None:
    """Refuse a viewpoint that asks for two things at once, before anything is started.

    A world-defined camera owns its own pose, so combining it with a free camera's angle is
    not a preference to resolve but a contradiction. Caught here rather than in the container:
    a simulator would reject it too, but only after an image pull, and its message would be
    about argv rather than about what the caller asked for.
    """
    if camera and (view or focus):
        raise ScreenshotUnavailable(
            f"camera={camera!r} renders through a camera the world defines, which owns its own "
            f"pose — so it cannot be combined with "
            f"{'view' if view else 'focus'}. Drop one of the two.")


def _entry(identity: dict, out_name: str, command: str, state_path: Path) -> dict:
    """The ``shell`` entry that renders the frame, in the campaign's own image.

    ``inputs[0]`` is the recording, which is why the backend spells it ``{inputs[0]}``: the
    generator stages each input into the container and substitutes the path it landed at. An
    absolute host path passes through ``inputs`` unchanged, so a run artifact needs no
    ``mount_at`` — unlike a campaign-file world, whose contents name siblings by the absolute
    path the run had, and which therefore has to go back at that same path.
    """
    inputs = [str(state_path)]
    entry = {"shell": {"out": out_name, "image": identity["image"], "command": command,
                       "inputs": inputs}}
    if identity.get("config_root"):
        inputs.append(identity["config_root"])
        entry["shell"]["mount_at"] = {identity["config_root"]: _RUN_FILE_MOUNT}
    return entry


def render(identity: dict, *, state_path: Path, at: Optional[float], view: dict, focus: list,
           camera: Optional[str], size: str, runner_context) -> Path:
    """Render one frame and return its path. The caller owns (and removes) its directory.

    *runner_context* is a zero-argument callable returning a **context manager** yielding the
    generator's ``container_runner_factory``, exactly as :func:`scene_cache.generate` takes
    one: the factory is backed by a pod, and whoever creates it has to close it.
    """
    from robovast.common.input_generation import \
        run_input_generators  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import \
        simulation_screenshot_command  # pylint: disable=import-outside-toplevel

    check_camera_args(view, focus, camera)
    if not state_path.is_file():
        raise ScreenshotUnavailable(
            f"this run recorded no state at {state_path.name}, so there is no moment to render. "
            "A recording is the simulator backend's to write, from the run's first sample on: "
            "a run still going is rendered from what it has recorded so far, and only a run "
            "that never reached its first sample leaves none.")

    execution = identity.get("execution") or {}
    name = identity.get("backend")
    try:
        command = simulation_screenshot_command(
            execution, state="{inputs[0]}", at=at, view=view, focus=focus, camera=camera,
            size=size)
    except Exception as err:  # noqa: BLE001 - reported as a reason, not a traceback
        raise ScreenshotUnavailable(
            f"the simulator backend {name!r} could not say how to render this run: {err}") from err
    if not command:
        raise ScreenshotUnavailable(
            f"this campaign's simulator ({name or 'none configured'}) renders no views. "
            "Re-rendering a run needs a simulator that can put its world back into a recorded "
            "state; a simulator RoboVAST only launches cannot. For a camera that was recorded "
            "during the run, use the run's video instead.")

    # A directory per request, owned by the caller: nothing here is worth keeping, and a
    # cache keyed on an arbitrary camera and time would never be hit twice.
    root = Path(tempfile.mkdtemp(prefix="robovast-screenshot-"))
    out_name = "render"
    try:
        with runner_context() as factory:
            run_input_generators(str(root), [_entry(identity, out_name, command, state_path)],
                                 progress_update_callback=logger.info,
                                 container_runner_factory=factory, use_cache=False)
    except Exception as err:  # noqa: BLE001 - every failure must reach the caller as a reason
        shutil.rmtree(root, ignore_errors=True)
        raise ScreenshotUnavailable(f"could not render this run: {err}") from err

    frame = root / out_name / FRAME_NAME
    if not frame.is_file():
        produced = sorted(p.name for p in (root / out_name).glob("*")) if (
            root / out_name).is_dir() else []
        shutil.rmtree(root, ignore_errors=True)
        raise ScreenshotUnavailable(
            f"the simulator reported success but wrote no {FRAME_NAME}"
            + (f" (it wrote: {', '.join(produced)})" if produced else ""))
    return frame


def discard(frame: Path) -> None:
    """Remove the directory :func:`render` created for *frame*.

    Named rather than inlined because the caller runs it *after* the response is sent, and a
    path built by hand at that distance from the one that made it is how a cleanup comes to
    delete the wrong tree.
    """
    root = frame.parent.parent
    if os.path.basename(root).startswith("robovast-screenshot-"):
        shutil.rmtree(root, ignore_errors=True)


def kept_root() -> Path:
    """Where kept renders live: ``ROBOVAST_SCREENSHOTS``, else ``~/.robovast/cache/screenshots``."""
    root = os.environ.get("ROBOVAST_SCREENSHOTS")
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".robovast", "cache", "screenshots")
    return Path(root)


def keep(campaign_id: str, frame: Path, *, now: Optional[float] = None) -> Path:
    """Move *frame*, fresh from :func:`render`, into the store; return where it is now.

    Its request directory is removed on the way, and the store is pruned, so keeping a render
    never grows the store past its bounds. The kept file's name is its name on the route.
    """
    if not _KEPT_CAMPAIGN.fullmatch(campaign_id):
        raise ScreenshotUnavailable(
            f"campaign id {campaign_id!r} cannot name a directory of the screenshot store")
    target = kept_root() / campaign_id / f"{uuid.uuid4().hex}.png"
    try:
        # Under the prune lock: a prune running between creating the campaign's directory and
        # writing into it would remove the directory as empty.
        with _prune_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(frame, target)
            _prune(now)
    finally:
        discard(frame)
    return target


def kept(campaign_id: str, name: str, *, now: Optional[float] = None) -> Path:
    """The kept render *name* of *campaign_id*, or ``KeyError`` saying it is not kept."""
    if not (_KEPT_CAMPAIGN.fullmatch(campaign_id) and _KEPT_NAME.fullmatch(name)):
        raise KeyError(f"no screenshot {name!r} of campaign {campaign_id!r}")
    path = kept_root() / campaign_id / name
    now = time.time() if now is None else now
    try:
        made = path.stat().st_mtime
    except OSError:
        made = None
    if made is None or now - made > KEEP_S:
        raise KeyError(
            f"screenshot {name} of campaign {campaign_id} is not kept: a render is kept for "
            f"{KEEP_S // 3600} h and at most {KEEP_MAX} are kept. Render it again with "
            "get_simulation_screenshot.")
    return path


def prune(*, now: Optional[float] = None) -> None:
    """Remove every kept render past :data:`KEEP_S`, then the oldest beyond :data:`KEEP_MAX`."""
    with _prune_lock:
        _prune(now)


def _prune(now: Optional[float]) -> None:
    """:func:`prune`, for a caller already holding the lock."""
    root = kept_root()
    now = time.time() if now is None else now
    entries = []
    for path in root.glob("*/*.png"):
        try:
            entries.append((path.stat().st_mtime, path))
        except OSError:
            continue                # removed by someone else since the glob saw it
    entries.sort()
    stale = [p for made, p in entries if now - made > KEEP_S]
    fresh = [p for made, p in entries if now - made <= KEEP_S]
    for path in stale + fresh[:max(0, len(fresh) - KEEP_MAX)]:
        path.unlink(missing_ok=True)
    for directory in root.glob("*"):
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            continue
