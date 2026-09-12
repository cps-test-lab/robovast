# sidecar image for robovast
#
# Pre-installed packages (beyond the Python standard library):
#   boto3                       — S3 access (MinIO) for s3_to_targz.py
#   pyyaml                      — read _transient/job_links.yaml to materialise
#                                 per-job symlinks in s3_to_targz.py / gcs_to_targz.py
#   pigz                        — parallel gzip (multi-core compression)
#   mc                          — MinIO client for S3 upload in post-run script

# Digest-pinned, for the reason the alpine/git pin above already gives: an untagged or
# merely tagged FROM resolves to whatever that tag points at on the day of the build, so two
# builds of the same commit can start from different bytes. This also freezes the apt state
# the base image itself baked in, which no amount of pinning *our* apt calls can reach.
# The tag is kept beside the digest because a bare digest tells a reader nothing about what
# it is. Refresh both with: make refresh-build-pins
FROM python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

RUN apk add --no-cache pigz==2.8-r1 \
 && pip install --no-cache-dir \
        boto3==1.42 google-auth==2.49.0 requests==2.32.5 paramiko==4.0.0 pyyaml==6.0.2

# TARGETARCH rather than a hard-coded linux-amd64, so the binary matches the image it
# lands in if this is ever built for another architecture.
ARG TARGETARCH
# mc is fetched from the project's GitHub release, PINNED and checksummed, because MinIO
# stopped serving it: dl.min.io answers 410 Gone for every community build, with "the open-source
# MinIO Server, MinIO Client (mc) and MinIO KES projects are archived and no longer maintained".
# That took every image here with it -- the URL had no version in it, so there was nothing to
# fall back to and nothing that said which build had been installed.
#
# The release assets survive the archive, so the fix is also an improvement: this pins a build
# and verifies it, where the old URL took whatever "latest" meant on the day and checked nothing.
# See MC_RELEASE / MC_SHA256_* below; both architectures are listed because the digest is what
# makes the pin worth having, and one sum cannot cover two binaries.
ARG MC_RELEASE=RELEASE.2025-08-13T08-35-41Z
ARG MC_SHA256_AMD64=01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891
ARG MC_SHA256_ARM64=14c8c9616cfce4636add161304353244e8de383b2e2752c0e9dad01d4c27c12c
RUN set -eu; \
    url="https://github.com/minio/mc/releases/download/${MC_RELEASE}/mc.linux-${TARGETARCH}.${MC_RELEASE}"; \
    case "$TARGETARCH" in \
      amd64) want="$MC_SHA256_AMD64" ;; \
      arm64) want="$MC_SHA256_ARM64" ;; \
      *) echo "no pinned mc checksum for TARGETARCH=$TARGETARCH" >&2; exit 1 ;; \
    esac; \
    wget -q "$url" -O /usr/local/bin/mc; \
    echo "$want  /usr/local/bin/mc" | sha256sum -c - ; \
    chmod +x /usr/local/bin/mc