ARG ROS_DISTRO=jazzy

FROM osrf/ros:${ROS_DISTRO}-desktop-full
SHELL ["/bin/bash", "-xo", "pipefail", "-c"]

WORKDIR /ws


##############################################################
####                        VNC                           ####
##############################################################
ARG NOVNC_VERSION=1.3.0
# hadolint ignore=DL3008
RUN apt-get update && apt-get install --no-install-recommends -y \
        x11vnc && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL "https://github.com/novnc/noVNC/archive/v${NOVNC_VERSION}.tar.gz" | tar -xzf - -C /opt && \
    mv -f "/opt/noVNC-${NOVNC_VERSION}" /opt/noVNC && \
    ln -snf /opt/noVNC/vnc.html /opt/noVNC/index.html && \
    # Use the latest Websockify source to expose noVNC
    git clone "https://github.com/novnc/websockify.git" /opt/noVNC/utils/websockify


##############################################################
####             Setup Software Rendering                 ####
##############################################################
# hadolint ignore=DL3008
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    nano vim \
    xserver-xorg-core \
    xvfb \
    mesa-utils \
    mesa-utils-extra \
    va-driver-all \
    xserver-xorg-input-all \
    xserver-xorg-video-all \
    mesa-vulkan-drivers \
    libvulkan-dev \
    openbox && \
    rm -rf /var/lib/apt/lists/* 

# Install system dependencies
# hadolint ignore=DL3008
RUN \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python3-pip \
        tk \
        python3-docker \
        ros-jazzy-moveit \
        ros-jazzy-moveit-resources \
        ros-jazzy-moveit-planners-chomp \
        ros-jazzy-controller-manager \
        ros-jazzy-gripper-controllers \
        ros-jazzy-joint-trajectory-controller \
        ros-jazzy-gz-ros2-control \
        ros-jazzy-ros2-control \
        ros-jazzy-moveit-msgs \
        ros-jazzy-py-trees-ros \
        ros-jazzy-py-trees-ros-interfaces \
        ros-jazzy-nav2-bringup \
        ros-jazzy-nav2-route \
        python3-autopep8 \
        python3-transforms3d \
        python3-antlr4 \
        clang-format \
        pylint \
        cppzmq-dev \
        python3-kubernetes \
        python3-pymongo \
        python3-gridfs \
        python3-defusedxml \
        ffmpeg \
        ros-jazzy-nav2-minimal-tb4-description \
        ros-jazzy-rmw-cyclonedds-cpp \
        ros-jazzy-rmw-zenoh-cpp \
        libgl1 && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /ws/src

# hadolint ignore=DL3003
RUN  git clone "https://github.com/cps-test-lab/scenario-execution.git" /ws/src/scenario-execution && \
     cd /ws/src/scenario-execution && \
     git checkout 25e09099e3b27e25331a7b04b8c588a37d9b536a && \
     . "/opt/ros/${ROS_DISTRO}/setup.bash" && \
     cd /ws && \
     colcon build

COPY container/files/startx11_virtual.sh /
COPY container/files/entrypoint.sh /

# Environment variables
ENV DISPLAY=:0
ENV XDG_RUNTIME_DIR=/tmp/runtime-user
ENV SIZEW=1600
ENV SIZEH=1200
ENV DPI=96
ENV CDEPTH=24
ENV QT_X11_NO_MITSHM=1

RUN mkdir -p /out && chmod 777 /out

ENTRYPOINT [ "/entrypoint.sh" ]