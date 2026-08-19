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
RUN wget -q "https://dl.min.io/client/mc/release/linux-${TARGETARCH}/mc" -O /usr/local/bin/mc && \
    chmod +x /usr/local/bin/mc