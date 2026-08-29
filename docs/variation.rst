.. _variation-points:

Variation Points
================

**A campaign varies three things.** An experiment has three parts, and each is a
configuration surface with an owner and a schema -- which is what lets a factor's
destination be checked before any compute is spent, and what decides which channel a value
belongs to.

.. list-table::
   :header-rows: 1
   :widths: 10 28 28 34

   * - Channel
     - What it varies
     - Owned by
     - Checked against
   * - ``scenario:``
     - the **trial** -- what happens during the run
     - scenario-execution
     - the parameters the ``.osc`` declares
   * - ``sim:``
     - the **world** -- what the trial runs in
     - the simulator backend
     - the backend's schema and the world itself
   * - ``sut:``
     - the **system under test** -- how the stack is configured
     - the stack
     - the config file the campaign declares

One ``configuration:`` block can use all three:

.. code-block:: yaml

   execution:
     containers:
       sut:
         config_files:
           nav2: files/nav2_params.yaml        # format inferred from the extension
           bt:   files/nav2_bt.xml

   configuration:
   - name: three-aspects
     variations:
     - ParameterVariationList:
         sim: components.floorplan.floor.friction     # the world
         values: [0.6, 1.4]
     - ParameterVariationList:
         sut: nav2.local_costmap.local_costmap.ros__parameters.inflation_layer.inflation_radius
         values: [0.30, 0.55]                          # the system under test
     - ParameterVariationList:
         scenario: goal_pose                           # the trial
         values: [...]

**Choosing between them is one question:** *who owns the schema this value is checked
against?* A key in a file the stack reads is ``sut:``; a value the ``.osc`` declares and
acts on -- including what it passes to ``ros_launch`` -- is ``scenario:``; a value the
simulator's configuration declares is ``sim:``.

That last point is worth stating because the boundary is not enforceable. A stack parameter
*can* be declared in the ``.osc`` and written into the stack's file at run time, and
RoboVAST cannot tell that such a parameter is stack configuration rather than trial
protocol. It is a rule you follow, not one you are stopped from breaking -- and the cost of
breaking it is that the value is checked by nobody, because the surface it belongs to was
never consulted.

See :ref:`the destination reference <config-variation-destination>` for the full rules, and
:ref:`the sut channel <sut-channel>` for what a config source is and how a format addresses
one.

The variation types below are available by default.

General
-------

.. variation-plugin:: robovast.common.variation.parameter_variation.ParameterVariationList

.. variation-plugin:: robovast.common.variation.parameter_variation.ParameterVariationDistributionUniform

.. variation-plugin:: robovast.common.variation.parameter_variation.ParameterVariationDistributionGaussian

.. variation-plugin:: robovast.common.variation.one_of_variation.OneOfVariation

Navigation
----------

.. variation-plugin:: robovast_nav.variation.floorplan_variation.FloorplanVariation

.. variation-plugin:: robovast_nav.variation.floorplan_variation.FloorplanGeneration

.. variation-plugin:: robovast_nav.variation.path_variation.PathVariationRandom

.. variation-plugin:: robovast_nav.variation.obstacle_variation.ObstacleVariation

.. variation-plugin:: robovast_nav.variation.obstacle_variation_with_distance_trigger.ObstacleVariationWithDistanceTrigger

.. variation-plugin:: robovast_nav.variation.path_variation.PathVariationRasterized



.. _variation-config-view:

Showing what a variation produced
---------------------------------

A variation that *places* something — obstacles at poses, a planned path, a start and a
goal — can say so, and the Config tab's :ref:`config view <config-view>` draws it. Answer
:meth:`~robovast.common.variation.base_variation.Variation.config_view_data` with neutral
geometry:

.. code-block:: python

   from robovast.common.scene_markers import ConfigViewContribution, SceneMarker

   class MyVariation(Variation):

       @classmethod
       def config_view_data(cls, config, base_path):
           placed = config["config"][config["_objects_parameter_name"]]
           return ConfigViewContribution(
               markers=[SceneMarker(kind="box", pos=[o.x, o.y], size=[0.5, 0.5, 1.0])
                        for o in placed],
               files={"map": config["config"].get("map_file", "")},
           )

**Geometry, not domain vocabulary.** A marker is a box at a pose, never "an obstacle" —
which is what lets the 3D scene, the 2D map and anything added later all draw a variation
they have never heard of. The kinds are ``box``, ``cylinder``, ``sphere``, ``pose``,
``path`` and ``point``; each carries an optional ``label``, ``color`` and ``group`` (markers
sharing a group are shown and hidden together, and it defaults to the variation's own name).
A marker with no geometry for its kind is refused rather than drawn as nothing.

``files`` names workspace-relative paths a panel may need — ``{"map": …}`` is what the
``map2d`` panel binds. Named rather than positional, so a variation with no map is simply
missing the key. Return the *relative* path: the browser fetches it through the workspace,
and an absolute path from the host that composed means nothing there. A campaign with no
map-producing variation is not stuck: the panel also takes a ``map:`` path declared on it in
the ``.vast`` (see :ref:`config-view`), which is the route for a map that is checked in.

Two properties worth knowing:

* It is a **pure function of the resolved configuration**, called after composition. It must
  not re-run the variation or touch the filesystem — which is what lets the answer be
  recomputed for whichever configuration the reader clicks, instantly, with nothing composed
  again.
* A hook that **raises is reported**, named, in the view, and the other variations still
  draw. A view that quietly lost one variation's markers would be indistinguishable from a
  variation that placed nothing.

This replaces the desktop editor's ``GUI_CLASS`` / ``GUI_RENDERER_CLASS``, where a variation
shipped a Qt widget and drew onto it imperatively. That tied every variation to one toolkit
and one 2D projection; returning data instead is what lets one contribution serve every
panel. ``robovast_nav``'s port lives in ``robovast_nav/config_view.py``.
