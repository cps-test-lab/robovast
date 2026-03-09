# sidecar image for robovast
#
# Pre-installed packages (beyond the Python standard library):
#   boto3                       — S3 access (MinIO) for s3_to_targz.py
#   pigz                        — parallel gzip (multi-core compression)
#   mc                          — MinIO client for S3 upload in post-run script

FROM python:3.12-alpine

RUN apk add --no-cache pigz==2.8-r1 curl==8.17.0-r1 \
 && pip install --no-cache-dir \
        boto3==1.42 google-auth==2.49.0 requests==2.32.5

RUN curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc && \
    chmod +x /usr/local/bin/mc