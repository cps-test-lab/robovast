# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A file still being written must not be published as if it were an artifact.

Every container in a scenario job mirrors the SAME shared ``/out/`` to the SAME prefix, each
one when *it* finishes (see the ``--overwrite`` note in ``_CLUSTER_POST_RUN_BLOCK`` and
``secondary_entrypoint.sh``). A container that stops while the simulator is still recording
therefore uploads roqsim's live sample stream, ``run.npz.part``.

``mc mirror`` has no ``--remove``, so that object outlives the file: the recorder unlinks the
stream when it packs ``run.npz`` at close, but the store keeps what it was already given. The
result was a permanent second copy of every successful run's samples -- one measured campaign
carried 336 of them, 158 MB, exactly one beside each ``run.npz``, which is the fingerprint of
this race rather than of interrupted runs.
"""

from robovast.common import execution

# roqsim.capture.STREAM_SUFFIX. Not imported: this package must not depend on a simulator
# plugin, and the coupling is a filename either way -- so it is named here, with the reason.
_STREAM_SUFFIX = ".part"


def test_the_results_mirror_excludes_in_progress_streams():
    script = execution._CLUSTER_POST_RUN_BLOCK

    mirror = next(line for line in script.splitlines()
                  if line.strip().startswith("mc mirror") and "/out/" in line)
    assert f"--exclude '*{_STREAM_SUFFIX}'" in mirror, (
        "an in-progress file uploaded by whichever container finished first is never "
        "removed again, so it has to be excluded rather than cleaned up afterwards")

    # The pattern has to reach NESTED streams -- a run's lives at <config>/<run>/run.npz.part,
    # never at the root -- so it relies on mc's `*` crossing `/`. Verified against the mc in
    # the deployed image: `--exclude "*.part"` drops cfg/0/run.npz.part and keeps run.npz.
    # `postprocess_job._mirror_excludes` leans on the same property for `_calibration/*`.
    assert mirror.count("--exclude") == 1


def test_the_executable_retag_skips_them_too():
    """The re-tag walks ``/out`` after the mirror and ``mc cp``s each hit onto itself.

    A stream excluded from the mirror has no object to re-tag, so leaving it in the walk
    would mean copying a key that is not there.
    """
    script = execution._CLUSTER_POST_RUN_BLOCK

    find = next(line for line in script.splitlines()
                if line.strip().startswith("find /out/"))
    assert f"-not -name '*{_STREAM_SUFFIX}'" in find


def test_the_local_lane_needs_no_exclusion():
    """Locally ``/out`` is a bind mount onto the host: nothing is uploaded, so nothing to
    exclude, and the recorder's own unlink is the whole story."""
    assert "mc mirror" not in execution._LOCAL_POST_RUN_BLOCK
