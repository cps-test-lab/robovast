#!/bin/bash
#
# Start PX4 SITL in its external-simulator configuration, against the roqsim MuJoCo simulator
# already listening on TCP 4560.
#
# This is the `px4` container's `command:`. It is a script rather than an argv in the .vast for a
# mechanical reason worth knowing before copying the pattern: RoboVAST hands a sidecar's command to
# `secondary_entrypoint.sh` as one string which is then word-split unquoted, so no single argument
# may contain a space. Anything that needs a shell needs a file.
#
# Mounted read-only at /config/files/px4_start.sh, because it is listed in the campaign's
# `run_files`. That is also how `10020_none_x500` reaches this container.

set -euo pipefail

log() { echo "px4_start: $*"; }

# ---------------------------------------------------------------------------------------------
# Locate PX4 inside the image.
#
# Searched rather than hard-coded. The path differs between a source build
# (build/px4_sitl_default/etc) and the project's published SITL image, and a wrong hard-coded path
# fails as "px4: not found" -- which reads as a broken image rather than as a moved directory. The
# probe is anchored on a file PX4 certainly ships (its own `none` airframe), so a hit is proof the
# ROMFS is there and not just that some directory named etc exists.
# ---------------------------------------------------------------------------------------------
AIRFRAMES="$(dirname "$(find / -path '*/init.d-posix/airframes/10016_none_iris' -print -quit 2>/dev/null || true)")"
if [ ! -d "${AIRFRAMES}" ]; then
    log "ERROR: no PX4 airframe directory in this image (looked for */init.d-posix/airframes/10016_none_iris)."
    log "       Either the image does not carry a PX4 ROMFS, or PX4 has renamed its 'none' airframe."
    exit 1
fi
ETC="$(dirname "$(dirname "${AIRFRAMES}")")"     # .../etc
ROOT="$(dirname "${ETC}")"                       # the directory PX4 expects as its working dir

PX4_BIN="$(command -v px4 || true)"
if [ -z "${PX4_BIN}" ] && [ -x "${ROOT}/bin/px4" ]; then
    PX4_BIN="${ROOT}/bin/px4"
fi
if [ -z "${PX4_BIN}" ]; then
    log "ERROR: no px4 binary on PATH and none at ${ROOT}/bin/px4."
    exit 1
fi
log "PX4 binary ${PX4_BIN}, ROMFS ${ETC}"

# ---------------------------------------------------------------------------------------------
# Install the airframe PX4 does not ship. See files/10020_none_x500 for why it has to exist:
# PX4's own x500 airframe enables the gz bridge and can therefore never reach the MAVLink HIL
# path this campaign attaches through.
#
# Copied rather than symlinked: the mount is read-only and PX4 reads the file through a shell
# `.` source, so a dangling link would surface as a startup that silently uses default parameters.
# ---------------------------------------------------------------------------------------------
if [ ! -r /config/files/10020_none_x500 ]; then
    log "ERROR: /config/files/10020_none_x500 is not mounted. It must be listed in execution.run_files."
    exit 1
fi
cp /config/files/10020_none_x500 "${AIRFRAMES}/10020_none_x500"
chmod 0644 "${AIRFRAMES}/10020_none_x500"
log "installed airframe 10020_none_x500"

# ---------------------------------------------------------------------------------------------
# Which simulator PX4 attaches to.
#
# `px4-rc.simulator` picks SIH for a sihsim_* model, gz when PX4_SIMULATOR=gz or SIM_GZ_EN is 1,
# jMAVSim for jmavsim_iris, and otherwise falls through to `px4-rc.mavlinksim` -- the MAVLink HIL
# client. `none_x500` hits nothing before that fallthrough, which is exactly the point of naming
# the airframe `none_*`. PX4_SIMULATOR is exported empty for the same reason: the published SITL
# image defaults to SIH, and an inherited value would silently win over the airframe.
#
# PX4_SYS_AUTOSTART selects the airframe by id and PX4_SIM_MODEL by name (PX4 resolves
# `[0-9]+_<name>` under the airframes directory and errors out by name if there is no match, which
# is the loud failure we want if the copy above ever stops working). Both are set because they are
# read at different points of rcS and agreeing is cheaper than depending on which one wins.
# ---------------------------------------------------------------------------------------------
export PX4_SIMULATOR=""
export PX4_SIM_MODEL=none_x500
export PX4_SYS_AUTOSTART=10020

# Where the simulator is. `px4-rc.mavlinksim` connects out to PX4_SIM_HOSTNAME, or to
# PX4_SIM_HOST_ADDR, or -- with neither set -- to localhost. Neither is set here, and that is
# deliberate rather than an omission: every container of a RoboVAST run shares one network
# namespace, so the roqsim container's TCP 4560 IS this container's localhost:4560. Setting a
# hostname would put a deployment's naming into a public example for no gain.
#
# The port is 4560 + instance, and this campaign runs one instance.

# rcS sources `px4-alias.sh` off the PATH on its 11th line, and the published image does NOT put
# PX4's own bin directory there -- `px4` itself is /usr/bin/px4, a different directory. Without
# this the run dies with `etc/init.d-posix/rcS: 11: .: px4-alias.sh: not found`, which reads as a
# corrupt image rather than as a PATH that is one entry short. (Measured against the pinned image.)
export PATH="${ROOT}/bin:${PATH}"

cd "${ROOT}"
log "starting PX4 SITL (airframe none_x500) -> MAVLink simulator on localhost:4560"
# The argument form, verified against the pinned image with `px4 --help` and by running it:
#
#     px4 [-h|-d] [-s <startup_file>] [<rootfs_directory>] [-w <working_directory>] ...
#
#   -d                  daemon mode: no interactive nsh shell. A container has no TTY, and without
#                       this PX4's `pxh>` prompt spams the log stream it shares with everything else.
#   <rootfs_directory>  POSITIONAL, and it is the ROMFS ROOT -- the parent of `etc`, not `etc`.
#                       rcS derives `${R}` from it and resolves every airframe and mixer path
#                       through that, so passing `etc` here makes PX4 look under `etc/etc`.
#   -w                  the working directory. `-s` defaults to `etc/init.d-posix/rcS` resolved
#                       against the CWD rather than against the rootfs, so the two must agree;
#                       setting -w is what makes the default startup file resolve.
#
# `-d "${ETC}"` was wrong on two counts and is worth recording, because it fails quietly rather
# than loudly: `-d` takes NO argument, so the value after it was consumed as the rootfs, and it was
# `etc` rather than its parent.
exec "${PX4_BIN}" -d -w "${ROOT}" "${ROOT}"
