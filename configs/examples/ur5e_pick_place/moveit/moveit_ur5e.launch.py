"""Bring up MoveIt 2 for the UR5e + Robotiq 2F-85 against a running roqsim bridge.

Built from PLAIN FILE PATHS rather than `MoveItConfigsBuilder`, because that helper resolves
everything against an installed ROS package's share directory and this configuration is generated
into the campaign's own `/config` tree, not into a package. Path-based is what lets
`ros_launch('', '<path>')` start it with no ament package to build or install -- the whole reason
this example ships no `ros2_ws/`.

`use_sim_time` defaults to true: the bridge publishes /clock and paces trajectories by sim time, so
a MoveIt on wall-clock mis-times every waypoint.
"""

from pathlib import Path

import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

HERE = Path(__file__).resolve().parent
#: Where the campaign's `execution.generate` step put the description -- beside this file, because
#: run files keep their project-relative layout under /config.
GEN = HERE / "gen"
URDF = GEN / "ur5e_2f85.urdf"
SRDF = GEN / "ur5e_2f85.srdf"


def _yaml(name):
    return yaml.safe_load((GEN / name).read_text())


def generate_launch_description():
    use_sim_time = LaunchConfiguration("use_sim_time")
    for path in (URDF, SRDF):
        # Fail at launch naming the path, not later with "no robot model" from move_group. These are
        # GENERATED (`roqsim export moveit`); a missing one means the generate step was skipped.
        if not path.exists():
            raise RuntimeError(f"{path} missing -- run `roqsim export moveit` first")

    robot_description = {"robot_description": URDF.read_text()}
    return LaunchDescription([
        DeclareLaunchArgument("use_sim_time", default_value="true"),
        Node(
            package="robot_state_publisher", executable="robot_state_publisher", output="screen",
            # The bridge publishes /joint_states but NOT the link transforms; without this
            # MoveIt has no idea where the links are.
            parameters=[robot_description, {"use_sim_time": use_sim_time}],
        ),
        # The one transform nothing else publishes. The bridge streams the box's and the bin's poses
        # in `world`, robot_state_publisher gives the arm's own tree below `base`, and the two trees
        # are otherwise unconnected -- so a tf2 lookup from `base` to the box fails and the trial
        # cannot see what it is reaching for. The arm is welded to the bench, so this transform is
        # constant, and it is the arm's spawn pose from the world YAML: +0.76 in z, and the UR
        # convention's half-turn about z that the model's root body carries.
        Node(
            package="tf2_ros", executable="static_transform_publisher", output="log",
            arguments=["--x", "0", "--y", "0", "--z", "0.76",
                       "--yaw", "3.14159265", "--pitch", "0", "--roll", "0",
                       "--frame-id", "world", "--child-frame-id", "base"],
            parameters=[{"use_sim_time": use_sim_time}],
        ),
        Node(
            package="moveit_ros_move_group", executable="move_group", output="screen",
            parameters=[
                robot_description,
                {"robot_description_semantic": SRDF.read_text()},
                {"robot_description_kinematics": _yaml("kinematics.yaml")},
                {"robot_description_planning": _yaml("joint_limits.yaml")},
                {"planning_pipelines": ["ompl"], "default_planning_pipeline": "ompl",
                 "ompl": _yaml("ompl_planning.yaml")},
                _yaml("moveit_controllers.yaml"),
                {"moveit_manage_controllers": True,
                 # The default 0.01 is too tight for a position servo that converges
                 # asymptotically: every execution then returns CONTROL_FAILED (-4).
                 "trajectory_execution.allowed_start_tolerance": 0.05,
                 "trajectory_execution.allowed_execution_duration_scaling": 2.0,
                 "trajectory_execution.allowed_goal_duration_margin": 5.0},
                {"use_sim_time": use_sim_time, "publish_robot_description_semantic": True},
            ],
        ),
    ])
