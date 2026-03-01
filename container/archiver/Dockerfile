# Archiver sidecar image for the robovast cluster pod.
#
# Pre-installed packages (beyond the Python standard library):
#   boto3                       — S3 access (MinIO) for s3_to_targz.py
#   google-auth                 — Google service-account authentication
#   google-api-python-client    — Google Drive upload API
#   pigz                        — parallel gzip (multi-core compression)
#
# The Nextcloud upload script uses only stdlib (urllib), so no extra
# packages are required for it.

FROM python:3.12-alpine

RUN apk add --no-cache pigz==2.8-r1 \
 && pip install --no-cache-dir \
        boto3==1.42 \
        google-auth==2.48 \
        google-api-python-client==2.190
