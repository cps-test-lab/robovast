# A probe, not a product: the smallest image that proves the snapshot recipe end to end.
FROM ubuntu:noble
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG UBUNTU_SNAPSHOT=20260618T000000Z
ARG ROS_SNAPSHOT=2026-06-18
ARG ROS_DISTRO=jazzy
ARG ROS_SNAPSHOT_KEY=AD19BAB3CBF125EA
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && rm -rf /var/lib/apt/lists/*
RUN printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' "$UBUNTU_SNAPSHOT" \
      > /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update && apt-cache policy tree | sed -n 's/^ *Candidate:/ubuntu-snapshot Candidate:/p' \
 && rm -rf /var/lib/apt/lists/*
RUN . /etc/os-release \
 && curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x$ROS_SNAPSHOT_KEY" \
      | gpg --dearmor -o /usr/share/keyrings/ros-snapshot.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/ros-snapshot.gpg] http://snapshots.ros.org/${ROS_DISTRO}/${ROS_SNAPSHOT}/ubuntu ${VERSION_CODENAME} main" \
      > /etc/apt/sources.list.d/ros2-snapshot.list \
 && apt-get update \
 && apt-cache policy ros-jazzy-ros-core | sed -n 's/^ *Candidate:/ros-snapshot Candidate:/p' \
 && rm -rf /var/lib/apt/lists/*
