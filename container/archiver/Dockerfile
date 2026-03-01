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

RUN apk add --no-cache pigz \
 && pip install --no-cache-dir \
        boto3 \
        google-auth \
        google-api-python-client
