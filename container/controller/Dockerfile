# robovast-controller image
#
# Hosts the in-cluster CampaignController for `vast exec cluster run` (search and
# batch). It contains robovast and its Python dependencies so the controller can
# compose, postprocess, evaluate and drive the search loop from inside the
# cluster — reading per-batch results from S3 in-cluster instead of round-tripping
# them to the laptop. Storage access (targz_to_s3.py / targz_to_gcs.py) uses the
# boto3 / google-cloud-storage already pulled in as robovast dependencies.
#
# It does NOT contain the ROS runtime: scenario execution and rosbag postprocessing
# run in the ROS robovast image (job pods / a ROS-enabled container), not here.
#
# robovast and robovast-nav (deps + a baseline copy of the code) are installed at
# build time. For fast dev iteration, `vast exec cluster run` builds wheels of the
# current robovast + robovast_nav source and `pip install --no-deps`'s them into
# the controller pod on top of this baseline.
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

# -- web UI (ui/dist), served by `vast serve` from this image -----------------
FROM node:20-slim AS ui-build
WORKDIR /ui
# Install deps against the lockfile first so this layer caches across UI edits.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build          # -> /ui/dist

FROM python:3.12-slim

# mc (MinIO client) + curl, for parity with the storage tooling used elsewhere.
# pigz: parallel gzip used by the in-controller upload-to-share compression
# (s3_to_targz.py / gcs_to_targz.py), which now runs here instead of in the
# removed archiver sidecar. tar ships in the base image.
#
# The remaining libs (libglib2.0-0, libegl1, libfontconfig1, libx11-6,
# libxkbcommon0, libgl1, libfreetype6, libdbus-1-3) are *not* for any display:
# nothing here opens a window or instantiates QApplication. They're the minimal
# shared-object set PySide6's bundled Qt6 libs dynamically link against at
# import time — required because robovast-nav's variation plugins (the `nav`
# extra below) import Qt-based GUI helpers at module level rather than lazily.
# Without them `import PySide6.QtWidgets` fails with e.g. "libGL.so.1: cannot
# open shared object file", which surfaces as nav variation types silently
# failing to register ("Unknown variation class"). Verified minimal/sufficient
# against pyside6-essentials + matplotlib's Qt backend on this base image.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates pigz git \
        libglib2.0-0 libegl1 libfontconfig1 libx11-6 \
        libxkbcommon0 libgl1 libfreetype6 libdbus-1-3 \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc \
 && chmod +x /usr/local/bin/mc

WORKDIR /opt/robovast

# Project metadata + source, then install robovast with its dependencies, the
# search-strategy extras the controller runs (qd → pyribs/shapely, optuna), and
# the nav extra (robovast-nav, a path dependency on src/robovast_nav) so the
# controller can resolve nav-specific variation plugins (FloorplanVariation,
# PathVariation*, ObstacleVariation*) when composing scenario variations
# in-cluster. The robovast(.gui) Qt extra itself is intentionally omitted
# (headless); rosbag postprocessing runs in the ROS image, not here. Note:
# robovast-nav currently hard-depends on pyside6-essentials (its variation
# classes import Qt-based helpers at module level, not lazily), so this extra
# does pull PySide6 into the image despite the controller being otherwise
# headless — and PySide6 ships no linux-aarch64 wheels.
COPY pyproject.toml poetry.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir poetry==1.8.2 \
 && poetry config virtualenvs.create false \
 && poetry install --only main --extras "qd optuna nav" --no-cache

# The built SPA + the env `robovast.service.app._ui_dist()` reads. The in-image
# path cannot be derived from the source tree (robovast is installed into
# site-packages here), so it is stated explicitly.
COPY --from=ui-build /ui/dist /opt/robovast/ui
ENV ROBOVAST_UI_DIST=/opt/robovast/ui

# Compatibility marker, kept in sync with COMPAT_VERSION in
# src/robovast/common/execution.py (same contract as the ROS image).
RUN echo "2" > /etc/robovast_compat_version

# This image is the robovast-service image (and the source of the robovast the
# postprocessing Job's initContainer copies in). The campaign controller is a
# library now, not an entrypoint — it runs inside the service process — so the
# default command is just a sane smoke check.
CMD ["vast", "--help"]
