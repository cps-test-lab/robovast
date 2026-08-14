# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The system under test: robot_state_publisher + nav2, and nothing else.

This is ``roqsim_nav2_example/nav2_turtlebot_depot.launch.py`` with its simulator half
removed. There, one launch started the MuJoCo bridge, the description and nav2 in a
single process tree because a single container ran all three. Here the simulator has a
container of its own (``containers.simulation``, from the roqsim backend), so what is
left is exactly the stack being measured -- which is the point of the split: the SUT can
be any nav2 image, and nothing roqsim-specific has to be installed in it.

It lives in the CAMPAIGN rather than in a package for the same reason. The SUT is
vanilla: RoboVAST layers ``scenario_execution_server`` onto whatever image the ``.vast``
names, and this file arrives through ``run_files`` at ``/config/files/``. A sidecar
receives the same ``/config`` mounts as the scenario container, so the scenario can
``ros_launch`` it over ``remote()``.

Started with an EMPTY package name and an ABSOLUTE path. With a package name a remote
``ros_launch`` would resolve against the *server's* ament index, and with a relative path
against ``ScenarioExecutionConfig().scenario_file_directory`` -- which server-side is the
client's directory evaluated on the server's filesystem, i.e. wrong in a way that only
shows up at run time.

What deliberately stays identical to the combined launch, so the two simulators remain
comparable by construction:

* ``nav2_minimal_tb4_description`` and not the fuller ``turtlebot4_description`` -- a
  different package is a different TF tree, which is the divergence this removes.
* ``bringup_launch.py`` taken with its own defaults (no ``use_composition``,
  ``use_respawn`` or ``namespace``), because that is what Gazebo's
  ``tb4_simulation_launch.py`` includes.
``autostart`` is where the two backends deliberately DIVERGE, and the default here stays
false only so that starting this file by hand does not activate nav2 against a simulator
that is not running yet. scenario_roqsim.osc passes ``true``: it gates the *launch* on
the simulator's first ``/clock`` and ``/scan``, so by the time nav2 starts, the thing it
would have raced is already publishing. Gating activation instead -- Gazebo's approach,
with ``autostart=false`` plus two ``service_call``s to the lifecycle managers -- cannot
work across the container boundary, because ``service_call`` fires ``call_async`` once
with no ``wait_for_service`` and its client has not finished matching a server in another
container; the request is dropped and the scenario parks forever. See the gate comment in
scenario_roqsim.osc.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart = LaunchConfiguration("autostart")
    params_file = LaunchConfiguration("params_file")
    map_yaml = LaunchConfiguration("map")

    robot_description_file = os.path.join(
        get_package_share_directory("nav2_minimal_tb4_description"),
        "urdf",
        "standard",
        "turtlebot4.urdf.xacro",
    )
    bringup = os.path.join(
        get_package_share_directory("nav2_bringup"), "launch", "bringup_launch.py"
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "use_sim_time",
                default_value="true",
                description="follow the simulator's /clock; the simulation container publishes it",
            ),
            DeclareLaunchArgument(
                "autostart",
                default_value="false",
                description="activate the nav2 lifecycle nodes at launch; false leaves them "
                "configured-but-inactive for the scenario's gate to start",
            ),
            DeclareLaunchArgument(
                "params_file",
                description="nav2 parameters -- the campaign pins /config/files/nav2_params.yaml "
                "so both simulators run byte-identical nav2 configuration",
            ),
            DeclareLaunchArgument(
                "map", description="occupancy map yaml, likewise pinned by the campaign"
            ),
            # Publishes base_link and everything below it (base_footprint, wheels, rplidar_link)
            # from the model, consuming the bridge's /joint_states over the shared network
            # namespace. The world sets `publish_static_tf: false` on the bridge precisely so these
            # come from here alone -- two publishers for one static transform is a TF conflict, not
            # redundancy. Without it, nav2's collision_monitor cannot transform its scan and, sitting
            # in the cmd_vel path, fails closed at zero velocity.
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                name="robot_state_publisher",
                output="screen",
                parameters=[
                    {
                        "use_sim_time": use_sim_time,
                        "robot_description": Command(["xacro ", robot_description_file]),
                    }
                ],
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(bringup),
                launch_arguments={
                    "map": map_yaml,
                    "params_file": params_file,
                    "use_sim_time": use_sim_time,
                    "autostart": autostart,
                }.items(),
            ),
        ]
    )
