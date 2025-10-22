from dataclasses import dataclass


@dataclass
class Position:
    """Represents a 2D position with x and y coordinates."""

    x: float
    y: float


@dataclass
class Orientation:
    """Represents an orientation in radians."""

    yaw: float


@dataclass
class Pose:
    """Represents a pose with position and orientation."""

    position: Position
    orientation: Orientation


@dataclass
class StaticObject:
    """Represents a static object with name, model, pose, and optional xacro arguments."""

    name: str
    model: str
    pose: Pose
    xacro_arguments: str = ""
