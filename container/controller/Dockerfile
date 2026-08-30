# robovast-controller image
#
# Hosts the in-cluster CampaignController for `vast workspace run` (search and
# batch). It contains robovast and its Python dependencies so the controller can
# compose, postprocess, evaluate and drive the search loop from inside the
# cluster — reading per-batch results from S3 in-cluster instead of round-tripping
# them to the laptop. Storage access (targz_to_s3.py / targz_to_gcs.py) uses the
# boto3 / google-cloud-storage already pulled in as robovast dependencies.
#
# It does NOT contain the ROS runtime: scenario execution and rosbag postprocessing
# run in the ROS robovast image (job pods / a ROS-enabled container), not here.
#
# Five distributions are installed at build time: robovast, its robovast-client,
# robovast-nav and robovast-sim-roqsim path dependencies, and robovast-cluster (the
# execution lane -- the only one needing its own step, added separately below).
# robovast-client is not optional and not an extra: it is the layer robovast is built on,
# so `poetry install` brings it in with the root project. There is no wheel-injection shortcut -- an earlier comment here
# described one that the launch path no longer does -- so changing any of them
# means rebuilding this image and rolling the Deployment with `vast service upgrade`.
# The cheaper loops are local `vast serve`, or `vast serve --backend cluster -x <ctx>`
# driving a real cluster from an editable checkout.
#
# Build context is the repository root (so pyproject + src are available):
#   docker build -f container/controller/Dockerfile -t <you>/robovast-controller:dev .
# or use container/controller/build.sh. Point a run at a dev image via the
# ROBOVAST_CONTROLLER_IMAGE env var.
#
# This image also runs the persistent `robovast-service` Deployment (mode 3), which
# serves the **web UI** alongside the REST API on one port — so the SPA is built
# here (stage below) and pointed at via ROBOVAST_UI_DIST. Without it the in-cluster
# service comes up API-only and the browser gets a 404 on `/`.

# -- web UI (frontend/ui/dist), served by `vast serve` from this image -------
# Digest-pinned, for the reason the alpine/git pin above already gives: an untagged or
# merely tagged FROM resolves to whatever that tag points at on the day of the build, so two
# builds of the same commit can start from different bytes. This also freezes the apt state
# the base image itself baked in, which no amount of pinning *our* apt calls can reach.
# The tag is kept beside the digest because a bare digest tells a reader nothing about what
# it is. Refresh both with: make refresh-build-pins
FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS ui-build
WORKDIR /ui
# Install deps against the lockfile first so this layer caches across UI edits.
COPY frontend/ui/package.json frontend/ui/package-lock.json ./
RUN npm ci
COPY frontend/ui/ ./
# @robovast/panel-kit is in-tree source, not an npm package -- resolved via a
# tsconfig/vite path alias at ../panel-kit relative to this WORKDIR, so it must
# land as a sibling of /ui for that alias to resolve inside the build.
COPY frontend/panel-kit/ /panel-kit
RUN npm run build          # -> /ui/dist

# -- robovast_nav's panel remote (costmap, map2d, nav2_behavior_tree) ---------
# Built here for the same reason the SPA is, and it is easy to miss: this bundle is
# *package data* of robovast-nav (pyproject `include = robovast_nav/web/dist/**`), it is
# git-ignored, and nothing in the Python install builds it. Without this stage the image
# ships whatever `dist/` happened to be lying in the build context -- a stale bundle from
# someone's laptop, or, from a clean clone, none at all: the service then serves the panel
# TYPE (the entry point is installed either way) with no assets behind it, and every nav
# panel in the run and config views fails to load at runtime.
#
# The directory layout is reproduced rather than flattened: the package's vite/tsconfig
# resolve @robovast/panel-kit at ../../../frontend/panel-kit relative to web/, so the two
# must sit where that alias points.
# Digest-pinned, for the reason the alpine/git pin above already gives: an untagged or
# merely tagged FROM resolves to whatever that tag points at on the day of the build, so two
# builds of the same commit can start from different bytes. This also freezes the apt state
# the base image itself baked in, which no amount of pinning *our* apt calls can reach.
# The tag is kept beside the digest because a bare digest tells a reader nothing about what
# it is. Refresh both with: make refresh-build-pins
FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS nav-panels-build
WORKDIR /build/src/robovast_nav/web
COPY src/robovast_nav/web/package.json src/robovast_nav/web/package-lock.json ./
RUN npm ci
COPY frontend/panel-kit/ /build/frontend/panel-kit/
COPY src/robovast_nav/web/ ./
RUN npm run build          # -> /build/src/robovast_nav/robovast_nav/web/dist

# Digest-pinned, for the reason the alpine/git pin above already gives: an untagged or
# merely tagged FROM resolves to whatever that tag points at on the day of the build, so two
# builds of the same commit can start from different bytes. This also freezes the apt state
# the base image itself baked in, which no amount of pinning *our* apt calls can reach.
# The tag is kept beside the digest because a bare digest tells a reader nothing about what
# it is. Refresh both with: make refresh-build-pins
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

# BuildKit supplies TARGETARCH, but only where it is declared. Without this the mc
# download below interpolates an empty string and 404s -- so it is declared here
# rather than left to the automatic build arg.
ARG TARGETARCH

# mc (MinIO client) + curl, for parity with the storage tooling used elsewhere.
# pigz: parallel gzip used by the in-controller upload-to-share compression
# (s3_to_targz.py / gcs_to_targz.py), which now runs here instead of in the
# removed archiver sidecar. tar ships in the base image.
#
# No X/GL shared objects here, deliberately. This image used to carry eight of
# them (libglib2.0-0, libegl1, libfontconfig1, libx11-6, libxkbcommon0, libgl1,
# libfreetype6, libdbus-1-3) for no display at all: they were the minimal set
# PySide6's bundled Qt6 libs had to dynamically link against just to be
# *imported*, because robovast-nav's variation plugins pulled a Qt map editor in
# at module level. Without them `import PySide6.QtWidgets` failed with
# "libGL.so.1: cannot open shared object file", which surfaced as nav variation
# types silently failing to register ("Unknown variation class"). The desktop
# tools are gone and nothing imports Qt any more, so the libs went with them.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates pigz git \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://dl.min.io/client/mc/release/linux-${TARGETARCH}/mc" -o /usr/local/bin/mc \
 && chmod +x /usr/local/bin/mc

WORKDIR /opt/robovast

# Project metadata + source, then install robovast with its dependencies, the
# search-strategy extras the controller runs (qd → pyribs/shapely, optuna), the
# nav extra (robovast-nav, a path dependency on src/robovast_nav) so the
# controller can resolve nav-specific variation plugins (FloorplanVariation,
# PathVariation*, ObstacleVariation*) when composing scenario variations
# in-cluster, and the roqsim extra (robovast-sim-roqsim, a path dependency on
# src/robovast_sim_roqsim) for the `backend: roqsim` entry point.
#
# The roqsim extra is here because a simulator backend has to be importable
# wherever a campaign is COMPOSED, and for `vast serve --attach` that is this
# image — the client side holds only a tunnel. Without it a campaign has to ship
# the backend itself in `plugins:`, which is a wheel to build per campaign. It
# costs nothing to carry: the backend is container specs and strings, its only
# dependency (pydantic) is already here, and it imports nothing from roqsim —
# no MuJoCo runtime enters this image. Contrast the nav extra below.
#
# Rosbag postprocessing runs in the ROS image, not here.
# Split into two layers so editing robovast/robovast_nav source doesn't force a
# full reinstall of the (large, slow) third-party dependency set.
#
# Layer 1 (heavy, rarely invalidated): install only the third-party dependencies.
# Its cache key is just pyproject.toml + poetry.lock, so source edits below don't
# touch it. --no-root skips the robovast project and --no-directory skips every
# path dependency (src/robovast_client, src/robovast_nav, src/robovast_sim_roqsim);
# all four are installed in layer 2.
COPY pyproject.toml poetry.lock ./
RUN pip install --no-cache-dir poetry==1.8.2 \
 && poetry config virtualenvs.create false \
 && poetry install --only main --extras "qd optuna nav roqsim analysis notebooks fair sftp" \
        --no-root --no-directory --no-cache

# Layer 2 (cheap, invalidated by any source edit): copy the source and install
# the four local packages (robovast root + the robovast-client, robovast-nav and
# robovast-sim-roqsim path deps) on top of the already-present dependencies.
# Poetry sees the heavy deps as satisfied, so this only builds/installs the four
# — fast.
COPY README.md ./
COPY src ./src
# The panel bundle, into the package that declares it as data -- so the wheel poetry builds
# below carries it. After `COPY src` (which would otherwise shadow it) and before the
# install (which is what packages it).
COPY --from=nav-panels-build /build/src/robovast_nav/robovast_nav/web/dist \
     ./src/robovast_nav/robovast_nav/web/dist
RUN poetry install --only main --extras "qd optuna nav roqsim analysis notebooks fair sftp" --no-cache

# The Kubernetes execution lane. It is a separate distribution that depends on robovast --
# not an extra of it -- so it cannot join the --extras string above and needs its own step.
# Without it the pod starts, serves, and then fails every create_campaign for want of a
# lane: the service comes up looking healthy and can run nothing. Its three dependencies
# (kubernetes, boto3, google-cloud-storage) resolve here rather than in layer 1, which
# costs a little cache; they are small next to the scientific stack layer 1 protects.
# Kept as its own layer against DL3059: consolidating with the RUN above is exactly what the
# comment rules out -- layer 1 exists to keep the scientific stack out of the cache key of
# everything that changes more often, and merging the two would put it back. The directive
# below has to be the last line before the instruction, so the reasoning goes here.
# hadolint ignore=DL3059
RUN pip install --no-cache-dir ./src/robovast_cluster

# The built SPA + the env `robovast.service.app._ui_dist()` reads. The in-image
# path cannot be derived from the source tree (robovast is installed into
# site-packages here), so it is stated explicitly.
COPY --from=ui-build /ui/dist /opt/robovast/ui
ENV ROBOVAST_UI_DIST=/opt/robovast/ui

# Compatibility marker, kept in sync with COMPAT_VERSION in
# src/robovast/common/execution.py (same contract as the ROS image).
RUN echo "2" > /etc/robovast_compat_version

# The revision this image's `COPY src` came from, captured here because this is the ONLY
# point at which it is knowable. `get_app_version` asks git, from the directory the module
# lives in -- which in this image is site-packages, with no `.git` anywhere above it, so the
# lookup cannot succeed however present the git binary is. Without this the service reported
# the package semver instead, which stays `2.0.0` across every edit and so could not answer
# the one question it is asked: is the change I just made loaded?
#
# Last, and deliberately: an ARG that changes on every commit invalidates every layer after
# it, so it goes after the expensive installs. Empty is honest -- `code_revision` then
# reports "" rather than a value that looks like a revision and is not.
#
# container/controller/build.sh (and therefore `make release-images`) passes this automatically
# from the checkout it lives in -- see container/image_stamp.sh. It did not, once, and the
# result was a locally released family whose deployed service could not say which code it ran.
# A `docker build` invoked by hand is the remaining case that has to pass it itself:
#   docker build -f container/controller/Dockerfile \
#     --build-arg ROBOVAST_GIT_REVISION="$(git rev-parse --short HEAD)$(git status --porcelain \
#       | grep -q . && echo +dirty)" .
ARG ROBOVAST_GIT_REVISION=""
ENV ROBOVAST_GIT_REVISION=${ROBOVAST_GIT_REVISION}

# When, for the same reason and with one difference. A revision answers "is my change loaded?";
# this answers "how old is what is deployed?" -- the question an operator looking at an upgrade
# button actually has, and one a revision cannot answer without a checkout to resolve it against.
# It is an ENV rather than only a label because a container cannot read its own labels, which is
# why the revision is baked this way too.
#
# The difference: a revision changes per commit, this changes per *build*, so it invalidates
# everything after it on every build. That is why it sits last -- what follows is an ENV and two
# LABELs, no install work. Empty is honest; `build_date()` then reports "" rather than a
# timestamp that describes nothing.
ARG ROBOVAST_BUILD_DATE=""
ENV ROBOVAST_BUILD_DATE=${ROBOVAST_BUILD_DATE}

# The same fact as a label, for a different reader. The ENV answers "which code is the running
# service executing?"; these answer "which controller is sitting in this registry?" -- readable
# with `buildx imagetools inspect` from a *remote* image, without pulling it or starting
# anything, which is what checking a project before pointing a cluster at it needs. Standard OCI
# names, as container/robovast/Dockerfile already uses. In CI docker/metadata-action supplies its
# own --label here (a full sha), and a CLI --label wins over this instruction; locally these are
# the only source.
LABEL org.opencontainers.image.source="https://github.com/cps-test-lab/robovast"
LABEL org.opencontainers.image.revision="${ROBOVAST_GIT_REVISION}"
LABEL org.opencontainers.image.created="${ROBOVAST_BUILD_DATE}"

# This image is the robovast-service image (and the source of the robovast the
# postprocessing Job's initContainer copies in). The campaign controller is a
# library now, not an entrypoint — it runs inside the service process — so the
# default command is just a sane smoke check.
CMD ["vast", "--help"]
