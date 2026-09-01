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

"""Package-provided web run-view panels shipped by ``robovast_nav``.

A panel type is a class registered in the ``robovast.panel_types`` entry-point group.
It declares three attributes (duck-typed, like variation types' ``WEB_PREVIEW``):

* ``TYPE`` -- the ``.vast`` ``visualization.panels`` type name (``- costmap:``).
* ``WEB_PANEL`` -- directory (relative to this module) holding the built Module-Federation
  bundle (``remoteEntry.js`` + chunks), served by the service at
  ``/panel_types/<name>/assets/...`` and loaded by the run view at runtime.
* ``PANEL_MODULE`` -- the exposed MF module the view renders.
* ``SURFACE`` (optional) -- which view the panel is for: ``"run"`` (the default, and what every
  panel was before there was a second surface) or ``"config"`` for the Config tab's column. One
  entry-point group and one asset route serve both; this is what tells them apart, and what makes
  a run panel named in ``visualization.config.panels`` a refusal rather than a blank panel.
* ``REMOTE_NAME`` (optional) -- the Module-Federation *container* name. Defaults to the
  entry-point name (one container per type). All panels here share a single ``robovast_nav``
  bundle (``robovast_nav/web`` exposes every ``PANEL_MODULE``), so they set the same
  ``REMOTE_NAME``; the service then points each type's asset URL at the one shared bundle.

The panels' React implementation lives in ``robovast_nav/web`` and is built into
``WEB_PANEL`` (shipped as package data). Each implements the run view's ``PanelProps``
contract (``{spec, clock, data}``), so it is time-synced and queries the run's results tables
exactly like a built-in panel.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from robovast.common.panel_bindings import Binding, DeclaredMarker

#: Shared Module-Federation container name for all robovast_nav panels (see vite.config.ts).
REMOTE_NAME = "robovast_nav"


class CostmapPanelType:
    """The nav2 costmap / occupancy-grid run-view panel (relocated here from the core UI).

    The only panel that consumes the service's dedicated ``/costmap`` endpoint and the
    nav occupancy-grid helpers, so it ships with the nav package rather than the core UI.
    """

    TYPE = "costmap"
    WEB_PANEL = "web/dist"
    PANEL_MODULE = "./costmap"
    REMOTE_NAME = REMOTE_NAME


class Map2DBindings(BaseModel):
    """What a ``.vast`` may say to :class:`Map2DPanelType`.

    Declared so the two keys this panel reads are checked, completed by the editor and described by
    ``get_plugin_details`` -- before, a misspelled ``map:`` validated cleanly and the panel simply
    drew no map, with nothing naming the key that was ignored.
    """

    model_config = ConfigDict(extra='forbid')

    # Descriptions rather than `#:` comments: pydantic does not read doc comments, and these are what
    # `get_plugin_details` shows an agent and the editor shows an author.
    map: Optional[Binding] = Field(
        None,
        description=(
            "The occupancy map to draw. A variation that generates one contributes it as the 'map' "
            "role (FloorplanGeneration, PathVariationRandom, PathVariationRasterized), and then "
            "this can be omitted. Bind it when the map is a fact the configuration does not carry: "
            "a checked-in file (map: files/depot.yaml) or the parameter holding it "
            "(map: {param: map_file})."))
    markers: list[DeclaredMarker] = Field(
        default_factory=list,
        description=(
            "Geometry this campaign declares itself, drawn beside whatever its variations "
            "contributed. In the MAP frame -- this panel is the map -- so a map-frame parameter "
            "needs no offset here, where the world-frame 3D scene needs one."))


class Map2DPanelType:
    """The occupancy map a nav campaign plans on -- a **config-view** panel.

    The direct replacement for the desktop config editor's map view, which is the one custom
    visualization that tool had. It ships here rather than in the core UI for the reason the
    costmap panel does: only a nav campaign has a ``map.yaml``, and only this package knows how
    to read one.

    It exists beside the 3D scene rather than being replaced by it because it is the *planning*
    view: a path is searched over these cells and an obstacle is placed relative to that path, so
    "why did the path go there" is a question about this picture rather than about the mesh.
    """

    TYPE = "map2d"
    SURFACE = "config"
    #: Same attribute a variation type uses, which is what makes ``get_plugin_details`` describe a
    #: panel's fields without knowing anything about panels.
    CONFIG_CLASS = Map2DBindings
    WEB_PANEL = "web/dist"
    PANEL_MODULE = "./map2d"
    REMOTE_NAME = REMOTE_NAME


class Nav2BehaviorTreePanelType:
    """The nav2 behavior-tree viewer: a live-updating, node-colored tree of nav2's BT.

    Renders the ``nav2_behaviors`` table (produced by this package's ``Nav2BtTree``
    postprocessing plugin, in the shared ``behaviors`` schema) via the generic ``data.series``
    seam -- no dedicated service endpoint. Structure comes from the BT XML; per-node status
    over time comes from nav2's ``/behavior_tree_log``.

    The panel *derives* from the host's built-in scenario tree rather than drawing its own:
    what nav2 needs is that renderer with a different table, title and empty-state hint. It
    exists as a type at all so a ``.vast`` can say ``- nav2_behavior_tree:`` and get those
    defaults -- and so the configs that already say it keep working.
    """

    TYPE = "nav2_behavior_tree"
    WEB_PANEL = "web/dist"
    PANEL_MODULE = "./behaviorTree"
    REMOTE_NAME = REMOTE_NAME
