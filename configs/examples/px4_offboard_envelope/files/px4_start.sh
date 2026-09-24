#!/bin/bash
# Start PX4 SITL against the roqsim simulator listening on TCP 4560. The `px4` container's command:
# a sidecar's argv is word-split unquoted, so anything with a space needs a script.

set -euo pipefail

log() { echo "px4_start: $*"; }

# The ROMFS path differs between a source build and the published image; anchor on a file PX4
# certainly ships.
AIRFRAMES="$(dirname "$(find / -path '*/init.d-posix/airframes/10016_none_iris' -print -quit 2>/dev/null || true)")"
if [ ! -d "${AIRFRAMES}" ]; then
    log "ERROR: no PX4 airframe directory in this image (looked for */init.d-posix/airframes/10016_none_iris)."
    exit 1
fi
ETC="$(dirname "$(dirname "${AIRFRAMES}")")"
ROOT="$(dirname "${ETC}")"

PX4_BIN="$(command -v px4 || true)"
if [ -z "${PX4_BIN}" ] && [ -x "${ROOT}/bin/px4" ]; then
    PX4_BIN="${ROOT}/bin/px4"
fi
if [ -z "${PX4_BIN}" ]; then
    log "ERROR: no px4 binary on PATH and none at ${ROOT}/bin/px4."
    exit 1
fi
log "PX4 binary ${PX4_BIN}, ROMFS ${ETC}"

# PX4 ships no external-simulator x500 airframe (see files/10020_none_x500). Copied, not linked:
# the mount is read-only and PX4 sources the file.
if [ ! -r /config/files/10020_none_x500 ]; then
    log "ERROR: /config/files/10020_none_x500 is not mounted. It must be listed in execution.run_files."
    exit 1
fi
cp /config/files/10020_none_x500 "${AIRFRAMES}/10020_none_x500"
chmod 0644 "${AIRFRAMES}/10020_none_x500"
log "installed airframe 10020_none_x500"

# A `none_*` model falls through px4-rc.simulator to the MAVLink HIL client, which connects to
# localhost:4560 when no PX4_SIM_HOSTNAME is set. PX4_SIMULATOR is cleared because the image
# defaults it to SIH.
export PX4_SIMULATOR=""
export PX4_SIM_MODEL=none_x500
export PX4_SYS_AUTOSTART=10020

# rcS sources px4-alias.sh from PATH, and the image does not put the ROMFS bin there.
export PATH="${ROOT}/bin:${PATH}"

cd "${ROOT}"
log "starting PX4 SITL (airframe none_x500) -> MAVLink simulator on localhost:4560"
# -d takes no argument; the positional is the ROMFS root (parent of etc), and -w must agree with
# it for the default startup file to resolve.
exec "${PX4_BIN}" -d -w "${ROOT}" "${ROOT}"
