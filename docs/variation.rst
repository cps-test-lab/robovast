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


