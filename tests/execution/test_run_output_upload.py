# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every container in a job mirrors the same /out to the same prefix.

They do not race for a file's *content* -- each writes its own log and its own resource
CSV, and uploads only after both have stopped. But the main container uploads while its
own log is still being appended, so every later uploader offers a LONGER version of the
shared ``_jobs/**`` files. Plain ``mc mirror`` refuses those ("Overwrite not allowed for
... (size)") and the store keeps the earliest truncated copy -- which is how an archived
``system.log`` came to end mid-sentence on its own "Mirroring /out/..." line, taking the
tail of every container's log with it. No run payload was lost, so nothing failed; the
logs just quietly stopped being usable for post-hoc diagnosis.

``container_runner`` already settled this question for the transfer it owns (see
``test_the_mirror_overwrites_rather_than_skipping_matching_files``); the run-output
upload was simply missed.
"""

import re

from robovast.common.execution import render_entrypoint

MIRROR = re.compile(r"^mc mirror\b(?P<flags>.*?)\s+/out/", re.MULTILINE)


def _upload_mirror() -> re.Match:
    """The ``mc mirror`` line of the cluster lane's /tmp/s3_upload.sh."""
    match = MIRROR.search(render_entrypoint(cluster=True))
    assert match, "the cluster post-run block does not mirror /out"
    return match


def test_the_run_output_mirror_overwrites():
    """Without this the LATER, more complete copy of a shared log is the one refused."""
    assert "--overwrite" in _upload_mirror().group("flags")


def test_the_local_lane_does_not_upload_at_all():
    """/out is a bind mount there, so there is nothing to mirror and no collision."""
    assert not MIRROR.search(render_entrypoint(cluster=False))
