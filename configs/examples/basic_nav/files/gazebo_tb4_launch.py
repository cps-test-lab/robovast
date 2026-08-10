# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The Gazebo half's bring-up: nav2's own tb4_simulation_launch.py + a ground-truth TF.

``tb4_simulation_launch.py`` is included **unmodified** — it is the reference Gazebo
bring-up and the thing this campaign compares robosito against, so it is not forked.
This wrapper adds only what the comparison needs on top and cannot get any other way,
because the campaign cannot edit an upstream launch file.

Why it exists: the campaign's analysis reads a ground-truth trajectory
(``<robot>_base_link_gt``), and Gazebo publishes no such frame by itself. robosito gets
it from the ``ground_truth_pose`` world plugin; here it comes from
``gazebo_tf_publisher`` (scenario-execution's gz helper, already installed in the
RoboVAST base image), which reads gz's own pose feed and republishes ``map -> …_gt``.
Without it the Gazebo runs record only ``base_link`` — the AMCL *estimate* — so a
"ground truth" comparison would silently be comparing believed poses.

Two values here are load-bearing and were both wrong in the obvious formulation:

* ``gz_pose_topic`` embeds the **world name**, and the Depot world is ``depot`` (see
  ``<world name="depot">`` in nav2_minimal_tb4_sim/worlds/depot.sdf) — not the
  ``default`` that most examples use. A wrong world name is not an error: the node
  subscribes happily and never receives a message, which looks exactly like the
  missing-frame bug it is here to fix.
* ``robot_name`` is forced to ``turtlebot4``. The publisher names the frame
  ``<gz model name>_<base_frame_id>_gt``, and tb4_simulation_launch.py's default model
  name is ``nav2_turtlebot4`` — which would emit ``nav2_turtlebot4_base_link_gt`` while
  robosito emits ``turtlebot4_base_link_gt``, leaving the two backends' trajectories
  under different table keys for no reason other than a default.

It also runs the campaign **headless**, which upstream cannot do while publishing ground
truth: ``headless`` gates the SceneBroadcaster plugin in the world *and* the Gazebo GUI,
so needing the pose feed forced a GUI into a headless cluster pod — ~480 s per trial
against robosito's ~107 s, an asymmetry with nothing to do with either simulator
(robosito's campaign runs with no viewer at all). ``files/depot_gt.sdf`` is upstream's
world with SceneBroadcaster made unconditional, which decouples the two; ``use_rviz`` is
off for the same reason, and because the robosito half has no viewer either.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# The gz world whose pose feed carries the robot: fixed by the map this campaign runs.
_WORLD = "depot"
# Must match the model name robosito's world gives the robot, so both backends publish
# one frame name and the analysis needs no per-backend special case.
_ROBOT_NAME = "turtlebot4"


def generate_launch_description():
    tb4_sim = os.path.join(
        get_package_share_directory("nav2_bringup"), "launch", "tb4_simulation_launch.py")
    # Beside this file: both are campaign run_files, mounted together under /config/files.
    world = os.path.join(os.path.dirname(os.path.abspath(__file__)), "depot_gt.sdf")

    return LaunchDescription([
        # Declared so the scenario's key_value arguments reach the include below. Defaults
        # mirror tb4_simulation_launch.py's own, so this file changes nothing when the
        # campaign passes nothing.
        DeclareLaunchArgument("map", default_value=""),
        DeclareLaunchArgument("params_file", default_value=""),
        DeclareLaunchArgument("headless", default_value="True"),
        DeclareLaunchArgument("autostart", default_value="true"),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(tb4_sim),
            launch_arguments={
                "map": LaunchConfiguration("map"),
                "params_file": LaunchConfiguration("params_file"),
                "headless": LaunchConfiguration("headless"),
                "autostart": LaunchConfiguration("autostart"),
                "robot_name": _ROBOT_NAME,
                # The world whose SceneBroadcaster is unconditional, so `headless:=True`
                # keeps the pose feed the ground-truth TF needs.
                "world": world,
                # No viewer in a campaign, on either backend.
                "use_rviz": "False",
            }.items(),
        ),
        Node(
            package="gazebo_tf_publisher",
            executable="gazebo_tf_publisher_node",
            name="gazebo_tf_publisher",
            output="screen",
            parameters=[{
                "gz_pose_topic": f"/world/{_WORLD}/dynamic_pose/info",
                "base_frame_id": "base_link",
                "use_sim_time": True,
            }],
        ),
    ])
