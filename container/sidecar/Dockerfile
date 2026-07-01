# sidecar image for robovast
#
# Pre-installed packages (beyond the Python standard library):
#   boto3                       — S3 access (MinIO) for s3_to_targz.py
#   pyyaml                      — read _transient/job_links.yaml to materialise
#                                 per-job symlinks in s3_to_targz.py / gcs_to_targz.py
#   pigz                        — parallel gzip (multi-core compression)
#   mc                          — MinIO client for S3 upload in post-run script

FROM python:3.12-alpine

RUN apk add --no-cache pigz==2.8-r1 \
 && pip install --no-cache-dir \
        boto3==1.42 google-auth==2.49.0 requests==2.32.5 paramiko==4.0.0 pyyaml==6.0.2

RUN wget -q https://dl.min.io/client/mc/release/linux-amd64/mc -O /usr/local/bin/mc && \
    chmod +x /usr/local/bin/mc