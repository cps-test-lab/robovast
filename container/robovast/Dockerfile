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
#
# This must be the digest of a multi-arch INDEX, never of one platform's manifest. `ros` publishes
# an index; `osrf/ros`, which this used to name, does not -- see the FROM below.
ARG ROS_BASE_DIGEST=sha256:2589a8fba5257307857890173c069852c2abf913a0be7970f172478baecb09e4

# Where scenario-execution comes from, defaulted HERE so this Dockerfile builds on its own.
#
# The pin must be a commit on a **durable** ref -- main, or a tag. It was previously a commit
# that only ever existed on a feature branch; once that branch was merged and deleted the
# commit became unreachable, and every build from a clean clone failed with "fatal: reference
# is not a tree". A pin whose commit can disappear is not a pin.
ARG SCENARIO_EXECUTION_REPO=https://github.com/cps-test-lab/scenario-execution.git
ARG SCENARIO_EXECUTION_REF=ac1759c7b3b80df5db5e82c42df957164adb7202

# The scenario-execution *server*, the second source this image bakes. Same idiom deliberately: a
# pin is only overridable if it is an ARG, and `make release-images-update-versions` finds a pin by
# that shape -- one written inline in the RUN that clones it is a pin nothing refreshes and nothing
# labels.
ARG SCENARIO_EXECUTION_SERVER_REPO=https://github.com/cps-test-lab/scenario-execution-server.git
ARG SCENARIO_EXECUTION_SERVER_REF=39c55d1081dcdd9f04f3762c0ec84b189c50b2da

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
# `ros`, the Docker Official image, and NOT `osrf/ros:${ROS_DISTRO}-desktop-full`, which this
# named until the arm64 build was made real.
#
# osrf/ros publishes each tag as a single amd64 manifest rather than an index. Docker does not
# refuse `--platform linux/arm64` against such a tag: it hands back the amd64 bytes and labels the
# result arm64. That built and pushed without complaint for as long as nothing executed in it --
# on an amd64 runner nothing is emulated, so even the RUN steps below passed -- and what shipped
# was an arm64 manifest full of amd64 userland, carrying arm64 `mc` and `fixuid` binaries fetched
# by TARGETARCH. It died with "Invalid ELF image for this architecture" the moment a real arm64
# host ran it. container/platforms.env had already recorded this failure for roqsim without the
# same conclusion being drawn here.
#
# `ros:${ROS_DISTRO}-ros-base` is a genuine multi-arch index (amd64 + arm64), so each platform's
# build starts from that platform's bytes. It carries less than desktop-full did; the difference is
# installed from the pinned snapshot below, which is strictly better for reproducibility -- those
# packages are now pinned to a dated archive rather than frozen at whenever the base was rebuilt.
FROM ros:${ROS_DISTRO}-ros-base@${ROS_BASE_DIGEST}
ARG ROS_DISTRO
ARG ROS_BASE_DIGEST
SHELL ["/bin/bash", "-xo", "pipefail", "-c"]
RUN test -d "/opt/ros/${ROS_DISTRO}" || { \
      echo "ERROR: ROS_BASE_DIGEST ${ROS_BASE_DIGEST} is not a ros:${ROS_DISTRO} image."; \
      echo "  It carries: $(ls /opt/ros 2>/dev/null | tr '\\n' ' ')"; \
      echo "  Docker ignores the tag when a digest is given, so the digest decides. Pass a"; \
      echo "  matching --build-arg ROS_BASE_DIGEST, or refresh both with make refresh-build-pins."; \
      exit 1; }

##############################################################
####            Point-in-time apt archives                ####
##############################################################
# Pinning apt *versions* is not enough to make a rebuild possible a year later: `apt-get install
# pkg=<ver>` fails as soon as the archive drops the superseded version, which both the Ubuntu and
# the ROS archives do continuously. What keeps an old version installable is a dated archive, so
# both are switched here -- before every apt-get call below, and before Dockerfile.roqsim's, which
# builds FROM this image and inherits these sources.
#
# The two dates are NOT the same point in time, and trying to make them one is what broke the
# first attempt at this. Each is bounded by something different:
#
#   ROS     a **fixed set** of dates, roughly quarterly; any other date 404s rather than resolving
#           to the nearest. So this one is discovered from what the server offers, never computed.
#   Ubuntu  any timestamp since 2023-03-01, but it must be **at or after the base image's own apt
#           state**. osrf/ros is rebuilt daily, so its packages are current; against an older
#           Ubuntu snapshot the archive offers `udev 255.4-1ubuntu8.16` while the base already
#           carries `libudev1 8.17`, and every install that pulls udev in (xserver-xorg-core, so
#           the software-rendering step) dies on "held broken packages". Derived from the base
#           image's creation timestamp for exactly that reason.
#
# `make refresh-build-pins` resolves both, each from its own source.
ARG UBUNTU_SNAPSHOT=20260819T003043Z
ARG ROS_SNAPSHOT=2026-06-18
# The dated ROS archive is signed by its own key, not the one the base image carries for the
# rolling repo. Pinned by full 40-hex fingerprint and verified after import, because a 64-bit key
# id is cheap to collide and this key decides what the entire ROS package set is trusted against.
# (The upstream key really does carry a misspelled "ROS Spanshot builder" uid beside the correct
# one. That is upstream, not a substitution.)
ARG ROS_SNAPSHOT_FPR=4B63CF8FDE49746E98FA01DDAD19BAB3CBF125EA
# grep -R, not -r: the base ships ros2.sources as a SYMLINK into /usr/share/ros-apt-source/, and
# -r skips symlinks found while recursing. With -r the rolling repo silently survives, apt then
# prefers whichever version is higher, and the snapshot pin quietly does nothing -- which is why
# this step ends by asserting on apt's own view rather than trusting the removal.
#
# Removal is by URL rather than by filename because the filename has already moved upstream once
# (ros2-latest.list -> ros2.sources), and guessing it wrong fails silently.
RUN . /etc/os-release \
 && grep -Rl -e archive.ubuntu.com -e security.ubuntu.com -e packages.ros.org \
      /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null | xargs -r rm -f \
 && printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: %s %s-updates %s-backports %s-security\nComponents: main universe restricted multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
      "${UBUNTU_SNAPSHOT}" "${VERSION_CODENAME}" "${VERSION_CODENAME}" "${VERSION_CODENAME}" \
      "${VERSION_CODENAME}" > /etc/apt/sources.list.d/ubuntu.sources \
 && curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x${ROS_SNAPSHOT_FPR}" \
      | gpg --dearmor -o /usr/share/keyrings/ros-snapshot.gpg \
 && gpg --show-keys --with-colons --with-fingerprint /usr/share/keyrings/ros-snapshot.gpg \
      | grep -q "^fpr:::::::::${ROS_SNAPSHOT_FPR}:" \
 && echo "deb [signed-by=/usr/share/keyrings/ros-snapshot.gpg] http://snapshots.ros.org/${ROS_DISTRO}/${ROS_SNAPSHOT}/ubuntu ${VERSION_CODENAME} main" \
      > /etc/apt/sources.list.d/ros2.list \
 && apt-get update \
 && if apt-cache policy | grep -E -q "archive\.ubuntu\.com|security\.ubuntu\.com|packages\.ros\.org"; then \
      echo "ERROR: a rolling apt archive survived the snapshot switch, so this build would not" >&2; \
      echo "       be reproducible. Surviving source(s):" >&2; \
      apt-cache policy | grep -E "archive\.ubuntu\.com|security\.ubuntu\.com|packages\.ros\.org" >&2; \
      exit 1; \
    fi

##############################################################
####          What the base no longer brings              ####
##############################################################
# The desktop-full package set, which used to arrive baked into osrf/ros:*-desktop-full. That tag
# has no arm64 build (see the FROM above), so the content is installed here instead -- from the
# snapshot pinned just above, which means these versions are now fixed by a dated archive rather
# than by whenever the base image was last rebuilt.
#
# `ros-${ROS_DISTRO}-desktop-full` is the same metapackage the old base installed, so this is the
# same set rather than a guess at it. Both architectures resolve it from the pinned ROS snapshot;
# the only packages that archive builds for amd64 alone are crocoddyl, mapviz and its plugins,
# multires-image and tile-map, none of which are pulled in here.
#
# It is its own layer, and the first heavy one, because it changes only when a pin above it does --
# so it stays cached across every change to the package lists further down.
# hadolint ignore=DL3008
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      "ros-${ROS_DISTRO}-desktop-full" && \
    rm -rf /var/lib/apt/lists/*

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
#
# `moveit-msgs` is MESSAGES, not servers: a campaign whose scenario drives move_group is a CLIENT, so
# it needs the message definitions and nothing else -- and this image is what such a container gets by
# default. move_group itself is in the roqsim flavour (Dockerfile.roqsim), because what executes a
# MoveIt trajectory here is roqsim's own bridge -- interfaces in the base, servers in the flavour.
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
        ros-${ROS_DISTRO}-moveit-msgs \
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

# Declared in the global scope above, like scenario-execution's, so the bare re-declaration at the
# LABEL near the end of this file resolves -- a bare ARG reads the global scope, not an earlier
# in-stage default.
ARG SCENARIO_EXECUTION_SERVER_REPO
ARG SCENARIO_EXECUTION_SERVER_REF
# No --depth: it used to be `--depth=1` followed by a checkout of a sha, which works only while
# that sha is still the branch tip. The moment main moves, a rebuild of THIS commit fails with
# "reference is not a tree" -- a pin that only resolves today is not a pin.
# hadolint ignore=DL3003
RUN  cd /ws && \
     git clone "$SCENARIO_EXECUTION_SERVER_REPO" /ws/src/scenario-execution-server && \
     cd /ws/src/scenario-execution-server && \
     git checkout "$SCENARIO_EXECUTION_SERVER_REF" && \
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
#
# Two sources for the revision, and not by accident: CI publishes with docker/metadata-action,
# whose generated --label carries the full sha and overrides this instruction, while a local
# build has no such thing and this ARG is the only source. So the ARG is not dead weight even
# though CI never passes it -- container/robovast/build.sh does, from the checkout it lives in
# (container/image_stamp.sh), and until it did a locally released image recorded no origin at all.
ARG ROBOVAST_GIT_REVISION=""
LABEL org.opencontainers.image.source="https://github.com/cps-test-lab/robovast"
LABEL org.opencontainers.image.revision="${ROBOVAST_GIT_REVISION}"

# The upstream sources baked in, so an image that has to be rebuilt can be rebuilt from the same
# ones. These are build ARGs and therefore invisible from outside the build -- without recording
# them, "which scenario-execution is in this image?" is answerable only by reading the Dockerfile
# at the commit above, and only if that commit is still reachable.
ARG SCENARIO_EXECUTION_REF
LABEL org.robovast.scenario-execution-ref="${SCENARIO_EXECUTION_REF}"
ARG SCENARIO_EXECUTION_SERVER_REF
LABEL org.robovast.scenario-execution-server-ref="${SCENARIO_EXECUTION_SERVER_REF}"

ARG TARGETARCH
RUN USER=ubuntu && \
    GROUP=ubuntu && \
    curl -SsL "https://github.com/boxboat/fixuid/releases/download/v0.6.0/fixuid-0.6.0-linux-${TARGETARCH}.tar.gz" | tar -C /usr/local/bin -xzf - && \
    chown root:root /usr/local/bin/fixuid && \
    chmod 4755 /usr/local/bin/fixuid && \
    mkdir -p /etc/fixuid && \
    printf 'user: %s\ngroup: %s\npaths:\n  - /\n  - /out\n' "$USER" "$GROUP" > /etc/fixuid/config.yml

# Check that what TARGETARCH fetched matches the userland it landed in.
#
# This is the only check that catches an image built for the wrong architecture, because such an
# image builds, pushes and inspects perfectly and fails when something finally runs it. Every
# other signal stays green. It is exactly how this image shipped an arm64 manifest full of amd64
# content for as long as it did.
#
# Cheap, and it belongs here rather than in CI: on a native runner every RUN above has already
# exec'd this architecture's binaries, so the base userland is covered by the build itself. What
# is NOT covered is a binary fetched by TARGETARCH and never run -- mc and fixuid, which were the
# two that really were arm64 while everything around them was amd64. Also protects a local build,
# which no CI check reaches.
#
# Compared against /bin/bash rather than against a table of architecture names: the question is
# whether these binaries match the userland they have to run in, and bash IS that userland. That
# needs no mapping to keep current, and it stays right for an architecture nobody has added yet.
# ELF e_machine is two little-endian bytes at offset 18 (x86-64 3e00, aarch64 b700); od is in
# coreutils, so this pulls in nothing.
#
# fixuid is inspected rather than run: it is setuid and its job is to change uid/gid and exec, so
# there is no harmless invocation (it takes only -q, and `-v` fails the flag parse). mc has a real
# one, so it gets exec'd as well as inspected.
RUN e_machine() { od -An -t x1 -j 18 -N 2 "$1" | tr -d ' '; } && \
    want=$(e_machine /bin/bash) && \
    for bin in /usr/local/bin/mc /usr/local/bin/fixuid; do \
      got=$(e_machine "$bin"); \
      if [ "$got" != "$want" ]; then \
        echo "ERROR: $bin is ELF e_machine $got, but this image's userland is $want." >&2; \
        echo "  A binary was fetched for TARGETARCH=${TARGETARCH} into a rootfs of another" >&2; \
        echo "  architecture, so the image would carry foreign content under a correct-looking" >&2; \
        echo "  label and die with 'Invalid ELF image' when run. A base whose tag is a single" >&2; \
        echo "  manifest rather than a multi-arch index does exactly this." >&2; \
        exit 1; \
      fi; \
    done && \
    mc --version > /dev/null && \
    echo "architecture check passed: TARGETARCH=${TARGETARCH}, uname -m=$(uname -m), e_machine=${want}"

USER ubuntu:ubuntu
