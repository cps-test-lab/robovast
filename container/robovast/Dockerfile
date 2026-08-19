ARG ROS_DISTRO=jazzy

# The ROS base, pinned by digest. Declared HERE, in the global scope, because an ARG used in a
# FROM is only substituted when it is declared before the first FROM -- mid-file it belongs to a
# stage and resolves to empty, which docker reports as `invalid reference format` on the bare
# trailing "@".
#
# Its own ARG rather than being inlined, because ROS_DISTRO is a real knob
# (`build.sh --ros-distro`) and **docker ignores the tag when a digest is given**. Inlined, the
# pin would silently win: `--ros-distro humble` would pull the jazzy bytes into an image whose
# name says humble. Overriding the distro therefore has to override the digest too, and the
# guard right after the FROM makes forgetting that loud instead of silent.
ARG ROS_BASE_DIGEST=sha256:d16957210572c5f919f3f9bd2fb0cc24a82542647489d8b8eda7c3beda31305f

# Where scenario-execution comes from, defaulted HERE so this Dockerfile builds on its own.
#
# The pin must be a commit on a **durable** ref -- main, or a tag. It was previously a commit
# that only ever existed on a feature branch; once that branch was merged and deleted the
# commit became unreachable, and every build from a clean clone failed with "fatal: reference
# is not a tree". A pin whose commit can disappear is not a pin.
ARG SCENARIO_EXECUTION_REPO=https://github.com/cps-test-lab/scenario-execution.git
ARG SCENARIO_EXECUTION_REF=bd77cc9b283f1b775240ab8ac65d71c761bd1e8a

# Pinned by digest, on the same reasoning the SCENARIO_EXECUTION_REF comment above gives:
# an untagged FROM resolves to whatever :latest points at on the day of the build, so two
# builds of the same commit can clone with different git versions. This is alpine/git
# v2.54.0. (hadolint DL3006 wants any explicit tag; a digest is the version that cannot
# be moved under us.)
FROM alpine/git@sha256:a299a963fbe31628ab481ca42907bcf0691d004f86734c02638abdf513691d42 AS scenario-execution-clone
ARG SCENARIO_EXECUTION_REPO
ARG SCENARIO_EXECUTION_REF
# No --depth: the ref is a bare commit sha, which a shallow clone cannot name. .git is dropped
# afterwards -- the colcon build below never reads it.
RUN git clone "$SCENARIO_EXECUTION_REPO" /src \
 && git -C /src checkout "$SCENARIO_EXECUTION_REF" \
 && rm -rf /src/.git

# The source tree alone, so that ``--build-context scenario-execution-src=<path>`` can replace
# it with a working checkout. That hatch is needed because the pin is a *release* control:
# iterating on scenario-execution means building from a commit that is not pushed yet, which a
# clone cannot do, and pushing to the shared repo first is a release decision made for the
# wrong reason.
FROM scratch AS scenario-execution-src
COPY --from=scenario-execution-clone /src /

# Digest-pinned, for the reason the alpine/git pin above already gives: an untagged or
# merely tagged FROM resolves to whatever that tag points at on the day of the build, so two
# builds of the same commit can start from different bytes. This also freezes the apt state
# the base image itself baked in, which no amount of pinning *our* apt calls can reach.
# The tag is kept beside the digest because a bare digest tells a reader nothing about what
# it is. Refresh both with: make refresh-build-pins
FROM osrf/ros:${ROS_DISTRO}-desktop-full@${ROS_BASE_DIGEST}
ARG ROS_DISTRO
ARG ROS_BASE_DIGEST
RUN test -d "/opt/ros/${ROS_DISTRO}" || { \
      echo "ERROR: ROS_BASE_DIGEST ${ROS_BASE_DIGEST} is not an osrf/ros:${ROS_DISTRO} image."; \
      echo "  It carries: $(ls /opt/ros 2>/dev/null | tr '\\n' ' ')"; \
      echo "  Docker ignores the tag when a digest is given, so the digest decides. Pass a"; \
      echo "  matching --build-arg ROS_BASE_DIGEST, or refresh both with make refresh-build-pins."; \
      exit 1; }
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

# From the ``scenario-execution-src`` stage, which is the pinned clone by default and a local
# working tree when a build overrides it (see the stage definition at the top of this file).
#
# This used to be ``COPY scenario-execution-src ...`` of a directory build.sh created, empty,
# in a temp context. Git cannot track an empty directory, so the COPY had nothing to resolve
# against in a plain checkout -- and CI builds with ``context: .``, which means this image's own
# workflow could never have produced it. A Dockerfile that only builds through one particular
# wrapper is a Dockerfile whose CI job is decorative.
COPY --from=scenario-execution-src / /ws/src/scenario-execution

# hadolint ignore=DL3003
RUN  cd /ws && \
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
#
# Written TWICE, on purpose. The label is what readers should use: `docker inspect` gets it
# without starting a container, and `buildx imagetools inspect` gets it from a *remote* image
# without pulling — which is what a pre-flight on a year-old campaign needs, since the question
# is whether those bytes are still obtainable at all. The file stays because every image built
# before the label existed carries only the file, and those are exactly the campaigns worth
# re-running; dropping it would make them unreadable to the very check meant to rescue them.
RUN echo "2" > /etc/robovast_compat_version
LABEL org.robovast.compat-version="2"

# Standard OCI provenance. These are the spec'd names — no reason to invent our own — and they
# are what lets a campaign's results say which commit its image was built from, so an image
# that has since been deleted can be rebuilt from the same source rather than guessed at.
ARG ROBOVAST_GIT_REVISION=""
LABEL org.opencontainers.image.source="https://github.com/cps-test-lab/robovast"
LABEL org.opencontainers.image.revision="${ROBOVAST_GIT_REVISION}"

# The upstream sources baked in, so an image that has to be rebuilt can be rebuilt from the same
# ones. These are build ARGs and therefore invisible from outside the build -- without recording
# them, "which scenario-execution is in this image?" is answerable only by reading the Dockerfile
# at the commit above, and only if that commit is still reachable.
ARG SCENARIO_EXECUTION_REF
LABEL org.robovast.scenario-execution-ref="${SCENARIO_EXECUTION_REF}"

ARG TARGETARCH
RUN USER=ubuntu && \
    GROUP=ubuntu && \
    curl -SsL "https://github.com/boxboat/fixuid/releases/download/v0.6.0/fixuid-0.6.0-linux-${TARGETARCH}.tar.gz" | tar -C /usr/local/bin -xzf - && \
    chown root:root /usr/local/bin/fixuid && \
    chmod 4755 /usr/local/bin/fixuid && \
    mkdir -p /etc/fixuid && \
    printf 'user: %s\ngroup: %s\npaths:\n  - /\n  - /out\n' "$USER" "$GROUP" > /etc/fixuid/config.yml

USER ubuntu:ubuntu
