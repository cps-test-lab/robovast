# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Running ``helm``, for whoever needs a chart.

These grew up in the Kueue module because Kueue was the first thing installed by chart, and
they are shared with the NVIDIA device-plugin installer. Neither is about Kueue: as long as
anything here is installed by chart, something has to run helm, and that outlives whatever is
admitting the jobs.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

def run_helm(args, check=True):
    """Run helm command. Returns (success, stderr)."""
    cmd = ["helm"] + args
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("Helm command failed: %s", result.stderr)
        if check:
            raise RuntimeError(
                f"Helm command failed: {result.stderr or result.stdout}"
            )
        return False, result.stderr or ""
    return True, ""

def helm_release_exists(release, namespace, ctx_helm):
    """Whether *release* is installed in *namespace*.

    Shared with the device-plugin installer so both take the same install-or-upgrade
    branch: a copy of this would be one place for the two to drift, and the whole point of
    the branch is that re-running setup must not fail on an already-installed chart.

    A helm that cannot answer reads as "not installed", which sends the caller down the
    ``install`` path -- and ``helm install`` on an existing release fails loudly instead of
    doing something surprising.
    """
    result = subprocess.run(
        ["helm", "list", "-n", namespace, "-q", "-f", release] + list(ctx_helm),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())
