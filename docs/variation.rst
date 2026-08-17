.. _variation-points:

Variation Points
================

RoboVAST supports plugin-provided variation types. The following are available by default.

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
and an absolute path from the host that composed means nothing there.

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
