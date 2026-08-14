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
# hadolint ignore=DL3008,SC2086
RUN \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python3-pip \
        tk \
        python3-docker \
        ros-${ROS_DISTRO}-joint-trajectory-controller \
        ros-${ROS_DISTRO}-gz-ros2-control \
        ros-${ROS_DISTRO}-ros2-control \
        ros-${ROS_DISTRO}-py-trees-ros \
        ros-${ROS_DISTRO}-py-trees-ros-interfaces \
        ros-${ROS_DISTRO}-nav2-bringup \
        ros-${ROS_DISTRO}-nav2-route \
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
        ros-${ROS_DISTRO}-nav2-minimal-tb4-description \
        ros-${ROS_DISTRO}-rmw-cyclonedds-cpp \
        ros-${ROS_DISTRO}-rmw-zenoh-cpp \
        libgl1 \
        tini \
        python3-zmq python3-msgpack && \
    rm -rf /var/lib/apt/lists/*

# Install MinIO client (mc) for S3 upload in post-run script.
# TARGETARCH (amd64 / arm64), not a hard-coded linux-amd64: this image is built for both,
# so the fixed URL put an x86 binary into every arm64 image.
ARG TARGETARCH
RUN curl -fsSL "https://dl.min.io/client/mc/release/linux-${TARGETARCH}/mc" -o /usr/local/bin/mc && \
    chmod +x /usr/local/bin/mc

RUN mkdir -p /ws/src

# Staged by build.sh, and empty unless it was asked for: a release build leaves this
# directory empty and the pinned clone below is what lands in the image. ``build.sh
# --scenario-execution-src <path>`` fills it with a working tree instead.
#
# The same shape as Dockerfile.roqsim's staged context, and for the same reason: a
# Dockerfile cannot branch on a build arg, so build.sh decides what is *in* the context and
# there is one COPY rather than two code paths for "where does the source come from".
#
# Why the hatch is needed at all: the pin is a release control, and iterating on
# scenario-execution means building an image from a commit that is not pushed yet -- which
# the clone cannot do. Without it the only way to test a change here is to push it to the
# shared repo first, which is a release decision made for the wrong reason.
COPY scenario-execution-src /tmp/scenario-execution-src

# The pin must be a commit on a **durable** ref -- main, or a tag. It was previously a
# commit that only ever existed on a feature branch; once that branch was merged and
# deleted the commit became unreachable, and every build of this image from a clean
# clone failed with "fatal: reference is not a tree". A pin whose commit can disappear
# is not a pin.
# hadolint ignore=DL3003
RUN  if [ -n "$(ls -A /tmp/scenario-execution-src 2>/dev/null)" ]; then \
       echo "scenario-execution: staged working tree (--scenario-execution-src)" && \
       cp -a /tmp/scenario-execution-src /ws/src/scenario-execution; \
     else \
       echo "scenario-execution: clone @ bd77cc9b283f1b775240ab8ac65d71c761bd1e8a" && \
       git clone "https://github.com/cps-test-lab/scenario-execution.git" /ws/src/scenario-execution && \
       cd /ws/src/scenario-execution && \
       git checkout bd77cc9b283f1b775240ab8ac65d71c761bd1e8a; \
     fi && \
     rm -rf /tmp/scenario-execution-src && \
     cd /ws && \
     git clone --depth=1 "https://github.com/cps-test-lab/scenario-execution-server.git" /ws/src/scenario-execution-server && \
     cd /ws/src/scenario-execution-server && \
     git checkout 39c55d1081dcdd9f04f3762c0ec84b189c50b2da && \
     cd /ws && \
     . "/opt/ros/${ROS_DISTRO}/setup.bash" && \
     colcon build

# Pip constraints every experiment image build inherits (PIP_CONSTRAINT is read by pip
# itself, so it applies to each install without a project repeating it).
#
# numpy<2 is an ABI floor of THIS image, not a preference: python3-transforms3d above is
# Debian-built against numpy 1.x and dies on numpy 2 with "np.maximum_sctype was removed
# in the NumPy 2.0 release". Without the constraint a dependency pulling numpy 2 breaks
# tf maths at runtime, long after the build looked fine. With it, pip steers the resolve
# instead (scipy>=1.13 lands on a 1.x-compatible scipy) or reports an honest conflict.
RUN mkdir -p /etc/robovast && \
    printf '# python3-transforms3d (apt, above) uses np.maximum_sctype, removed in numpy 2\nnumpy<2\n' \
        > /etc/robovast/pip-constraints.txt
ENV PIP_CONSTRAINT=/etc/robovast/pip-constraints.txt

# Environment variables
# Unbuffered: a campaign's only view of a run is its streamed log, and a buffered
# interpreter withholds output for as long as it likes -- including the traceback of a
# crash, which is exactly when the log is read.
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:0
ENV XDG_RUNTIME_DIR=/tmp/runtime-user
ENV SIZEW=1600
ENV SIZEH=1200
ENV DPI=96
ENV CDEPTH=24
ENV QT_X11_NO_MITSHM=1

RUN mkdir -p /out && chmod 777 /out

# Compatibility version — must match COMPAT_VERSION in
# src/robovast/common/execution.py.  Read by all execution contexts
# (local, cluster, postprocessing) to detect host/container mismatches.
RUN echo "2" > /etc/robovast_compat_version

ARG TARGETARCH
RUN USER=ubuntu && \
    GROUP=ubuntu && \
    curl -SsL "https://github.com/boxboat/fixuid/releases/download/v0.6.0/fixuid-0.6.0-linux-${TARGETARCH}.tar.gz" | tar -C /usr/local/bin -xzf - && \
    chown root:root /usr/local/bin/fixuid && \
    chmod 4755 /usr/local/bin/fixuid && \
    mkdir -p /etc/fixuid && \
    printf 'user: %s\ngroup: %s\npaths:\n  - /\n  - /out\n' "$USER" "$GROUP" > /etc/fixuid/config.yml

USER ubuntu:ubuntu
