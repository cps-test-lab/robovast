
Setup
=====

Installation with ROS 2
-----------------------

Prerequisites
^^^^^^^^^^^^^

Install ROS2 following the `installation instructions <https://docs.ros.org/en/jazzy/Installation.html>`_ for your distribution `$ROS_DISTRO`.

RoboVAST currently supports the ROS 2 distribution `Jazzy <https://docs.ros.org/en/jazzy/index.html>`_.

Installation
^^^^^^^^^^^^

Clone the RoboVAST repository

.. code-block:: bash

   git clone https://github.com/cps-test-lab/robovast.git

and install the necessary dependencies

.. code-block:: bash

   rosdep install  --from-paths . --ignore-src
   pip3 install -r requirements.txt

Now, build your workspace by running

.. code-block:: bash

   colcon build --symlink-install

and source your installation by running

.. code-block:: bash

   source /opt/ros/$ROS_DISTRO/setup.bash && source install/setup.bash
