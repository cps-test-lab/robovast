#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Is the local Docker lane usable? A preflight for ``vast init``.

Asks the ``docker`` **CLI**, not the Python SDK, because that is what actually runs a
campaign: the local lane shells out to ``docker compose`` and ``docker buildx`` throughout
(``docker_exec_lane``, ``execute_local``, ``image_build``). Checking through the SDK could
therefore pass while the thing the lane really needs was missing, and it cost a hard
dependency on ``docker`` for a check on one command.
"""

import logging
import shutil
import subprocess  # nosec B404 - the docker CLI is what the local lane itself runs

logger = logging.getLogger(__name__)


def check_docker_access():
    """Check that the Docker CLI is present and its daemon answers.

    Returns:
        tuple: (bool, str) — (success, message). The message names the remedy on
        failure, since this runs as a preflight and the caller only relays it.
    """
    if shutil.which("docker") is None:
        logger.warning("the docker CLI is not on PATH")
        return False, ("the 'docker' command is not on PATH, and the local lane runs "
                       "campaigns through it. Install Docker, or pass --force to skip "
                       "this check.")
    try:
        logger.debug("Checking Docker daemon access")
        # `docker version` (not `--version`) talks to the daemon; `--version` prints the
        # client's version happily while the daemon is down, which is the state that
        # actually breaks a run.
        out = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("Failed to check Docker access: %s", e)
        return False, f"Failed to check Docker access: {e}"

    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        reason = detail[0] if detail else "no reason given"
        logger.warning("Docker daemon is not accessible: %s", reason)
        return False, f"Docker daemon is not accessible: {reason}"

    version = out.stdout.strip() or "unknown"
    logger.debug("Docker is accessible (version %s)", version)
    return True, f"Docker is accessible (version {version})"
