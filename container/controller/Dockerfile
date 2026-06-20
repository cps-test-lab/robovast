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
# robovast (deps + a baseline copy of the code) is installed at build time. For
# fast dev iteration, `vast exec cluster run` copies the current source as a wheel
# into the controller pod and `pip install --no-deps`'s it on top of this baseline.
#
# Build context is the repository root (so pyproject + src are available):
#   docker build -f container/controller/Dockerfile -t <you>/robovast-controller:dev .
# or use container/controller/build.sh. Point a run at a dev image via the
# ROBOVAST_CONTROLLER_IMAGE env var.

FROM python:3.12-slim

# mc (MinIO client) + curl, for parity with the storage tooling used elsewhere.
# pigz: parallel gzip used by the in-controller upload-to-share compression
# (s3_to_targz.py / gcs_to_targz.py), which now runs here instead of in the
# removed archiver sidecar. tar ships in the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates pigz \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc \
 && chmod +x /usr/local/bin/mc

WORKDIR /opt/robovast

# Project metadata + source, then install robovast with its dependencies and the
# search-strategy extras the controller runs (qd → pyribs/shapely, optuna). The
# Qt GUI extra is intentionally omitted (headless); rosbag postprocessing runs in
# the ROS image, not here.
COPY pyproject.toml poetry.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[qd,optuna]"

# Compatibility marker, kept in sync with COMPAT_VERSION in
# src/robovast/common/execution.py (same contract as the ROS image).
RUN echo "2" > /etc/robovast_compat_version

# The controller pod overrides the command (e.g. an idle `sleep infinity` so the
# host can copy in the dev wheel and exec the controller); provide a sane default.
CMD ["python", "-m", "robovast.execution.controller", "--help"]
