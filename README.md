# RoboVAST #

*Variation Automation and Scalable Testing for Robotic Systems*

> **Disclaimer:** RoboVAST is currently being released. The source code and resources are being published progressively until our presentation at [ROSCon DE 2025](https://roscon.ros.org/de/2025/)


**RoboVAST** is an open-source framework for automated, large-scale integration testing of robotic software in simulated environments. Built upon proven foundations including the floorplan-dsl for parameterizable indoor environment generation, scenario-execution for single test execution, and Kubernetes for orchestration, RoboVAST enables developers to systematically validate their systems across thousands of varied test scenarios.

**Mobile robot navigation serves as our primary use case**, focusing on indoor environments where robots must navigate with varying layouts, tasks, obstacles, navigation parameters and environmental conditions. This foundational application demonstrates RoboVAST's core capabilities while providing immediate value to the mobile robotics community.

RoboVAST provides a **comprehensive dataset** designed to test multiple aspects of **mobile robot software** like Nav2, including localization, path planning, obstacle avoidance, and dynamic re-planning. This reference dataset can be used out-of-the-box or adapted to specific user requirements, significantly lowering the barrier to robust robotics testing.


## Framework Architecture ##

![Framework Overview](docs/images/overview.png)

### Variation ###

RoboVAST combines multiple variation dimensions to generate comprehensive test suites. Environment generation uses the floorplan-dsl to describe and generate diverse 3D indoor environments with parametric variation of room dimensions and connectivity. For systematic variation, it combines parameters specific to the use case, such as start/end poses, obstacle configurations, and sensor noise for mobile robot navigation. The modular architecture supports extensible addition of new variation dimensions based on specific application requirements. CLI- and GUI-based variant creation and selection tools are available.

### Execution ###

RoboVAST orchestrates test execution by creating Kubernetes jobs that run individual robot simulations using scenario-execution, including screen capturing and ROS bag data collection. The platform handles parallel deployment across available cluster nodes along with required input and output data management. This architecture enables execution of multiple tests in parallel depending on cluster size, significantly reducing validation time while ensuring reproducible execution across distributed computational environments.

### Analysis ###

To support basic analysis of test results, the framework provides GUI and CLI tools with automated playback, trajectory visualization, and AI-assisted log analysis of each run. These tools offer fundamental analysis capabilities including performance metric extraction, failure detection, and visual inspection of robot behavior.

For use case-specific analysis requirements, users can implement custom analysis workflows. The framework includes examples for further analysis workflows, enabling users to perform domain-specific evaluation using their preferred analysis tools and methodologies.

## Mobile Robot Reference Dataset ##

The RoboVAST dataset for mobile robots comprises thousands of mobile robot navigation tests conducted in Gazebo with ROS2 across diverse indoor environments and conditions.

The dataset serves as a comprehensive validation tool for navigation stacks such as Nav2, enabling developers to identify fundamental issues such as incorrect parametrization, setup problems, or software bugs. By testing their Nav2 configuration against the reference dataset, users can quickly assess system correctness and evaluate overall performance characteristics.