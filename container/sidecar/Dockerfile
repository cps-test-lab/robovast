# sidecar image for robovast
#
# The helper every pod the service launches moves its bytes with: the init container that
# lands a job's inputs (`curl | tar` from the service's data plane), the container that
# delivers its outputs (`tar | curl` into it), and the context fetch of an image build.
# Nothing in a pod reaches storage by any other route, so the image carries exactly the
# two tools those pipelines are made of (robovast/execution/cluster_execution/pod_access.py):
#   curl                        — the HTTP half; `-f` turns a refused response into an
#                                 exit status the pipeline's caller can read
#   tar (GNU)                   — the archive half; the pipelines use `--exclude`, which
#                                 busybox's applet lacks, so GNU tar is installed over it

# Digest-pinned: an untagged or merely tagged FROM resolves to whatever that tag points at
# on the day of the build, so two builds of the same commit can start from different bytes.
# This also freezes the package index the base image itself baked in, which no amount of
# pinning *our* apk calls can reach. The tag is kept beside the digest because a bare digest
# tells a reader nothing about what it is. Refresh both with: make refresh-build-pins
FROM python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

# A pipe reports the LAST command's status without this, so a version piped into a check
# passes when the thing producing it failed. Alpine's shell is ash, which has no bash to name.
SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# Versions pinned to what the pinned base's package index serves, so the image is the same
# bytes on every build; refresh them with the base:
#   docker run --rm <base> sh -c 'apk update >/dev/null && apk policy curl tar'
RUN apk add --no-cache curl==8.22.0-r0 tar==1.35-r5

# GNU tar must be the `tar` a pipeline finds, not busybox's applet: the package installs it
# over the applet's path, and this is what makes sure of it.
RUN tar --version | head -n 1 | grep -q 'GNU tar' \
 && curl --version | head -n 1
