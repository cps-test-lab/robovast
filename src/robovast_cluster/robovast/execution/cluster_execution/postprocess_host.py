#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
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
"""Run the pure-Python postprocessing stage against a staged campaign, in the pod.

Run as the postprocessing Job's main container, after
:mod:`.postprocess_stage` has staged the campaign (and after the optional
conversion container has produced its CSVs)::

    python3 -m robovast.execution.cluster_execution.postprocess_host

It runs beside the data instead of fetching it: the same
:func:`postprocess_job.run_host_postprocessing` the off-cluster lane calls, so there is one
implementation of the sequence, but with the campaign already on local disk. What it derives
then has to be sent back, because this pod's filesystem does not outlive it -- the object
store is the campaign's durable home.
"""

import json
import logging
import os
import sys

from . import postprocess_usage
from .postprocess_stage import ENV_CAMPAIGN_ID, ENV_STAGE_DEST, cluster_config_from_env

logger = logging.getLogger(__name__)

#: ``"1"`` bypasses the step caches, re-deriving what a previous run already produced.
ENV_FORCE = "ROBOVAST_POSTPROCESS_FORCE"

#: Comma-separated postprocessing step names to skip, on top of the rosbag steps
#: :func:`postprocess_job.run_host_postprocessing` always skips (the conversion container
#: owns those).
ENV_SKIP = "ROBOVAST_POSTPROCESS_SKIP"

#: JSON list of ``search.postprocessing`` commands to run instead of the campaign-level
#: pass. Set only on a per-batch Job.
#:
#: A search's batch and a finished campaign run DIFFERENT lists -- ``search.postprocessing``
#: and ``results_processing.postprocessing`` are separate blocks of the ``.vast`` -- so a
#: batch cannot be expressed as "the campaign pass, with the completing steps off": it would
#: run the wrong list. The controller has already resolved which commands are this batch's,
#: so they are passed rather than looked up again here.
#:
#: Running them is also all a batch does. Unset, the host runs the campaign-level pass and
#: completes the campaign -- index ingest, metadata, provenance record -- which a search
#: reaching this once per batch must not do to a campaign that is still growing.
ENV_COMMANDS = "ROBOVAST_POSTPROCESS_COMMANDS"


#: Files that appear in the staged tree but are not the campaign's, so they must not be
#: written back into its durable home.
#:
#: The stat-diff below treats anything new as derived output, which is right for everything
#: the stages produce and wrong for a scratch file. ``rosbags_process`` keeps its per-bag
#: hash cache beside the bag it describes, and that path is fixed in the script with no
#: override -- so it is filtered here rather than relocated. It is rebuildable by definition
#: and describes a pod that no longer exists, so uploading it would add a file per bag to
#: every campaign for a cache no later reader can use.
NOT_CAMPAIGN_DATA = frozenset({".robovast_rosbags_process_cache"})


def _snapshot(root: str) -> dict:
    """Map every file under *root* to ``(size, mtime_ns)``.

    Taken before the host stage runs, which is enough to identify what it derived: this
    container starts only once both initContainers have finished, so everything already on
    disk came from the store or from the conversion, and any file that appears or changes
    afterwards is this stage's output. That is what keeps the upload proportional to what
    was produced rather than to the campaign that was staged.

    Broken symlinks are skipped -- an interrupted campaign can leave a ``job`` link whose
    target was never produced, and ``os.walk`` reports it as a file.
    """
    seen = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            seen[path] = (st.st_size, st.st_mtime_ns)
    return seen


def _upload_derived(cluster_config, campaign_id: str, campaign_root: str,
                    before: dict) -> int:
    """Send this stage's outputs back to the campaign's durable home; return the count.

    ``_execution/`` goes wholesale, because it is the campaign's account of itself: the
    POSTPROCESSING section of the campaign log *is* ``_execution/postprocessing.log`` in the
    store, so until this has run the account exists nowhere a reader can see it. Everything
    else is uploaded only where it differs from the snapshot, so the staged run data is not
    written back over itself.

    What went up is then named in :data:`~.postprocess_job.OUTPUT_MANIFEST`, **written here
    because this is the only place that knows it**: the service fetches these objects back
    by reading that one key, rather than listing a campaign prefix whose bulk is the rosbags
    it does not want. It is uploaded LAST, after the objects it names, so a manifest can
    never advertise something that is not in the store yet.
    """
    from . import in_pod_storage  # noqa: PLC0415
    from .postprocess_job import publish_execution_dir  # noqa: PLC0415

    publish_execution_dir(cluster_config, campaign_id, campaign_root)

    bucket, prefix = in_pod_storage.campaign_storage_location(cluster_config, campaign_id)
    storage = in_pod_storage.storage_client_for(cluster_config)
    execution_dir = os.path.join(campaign_root, "_execution")
    sent = []
    for path, stamp in _snapshot(campaign_root).items():
        if path.startswith(execution_dir + os.sep) or before.get(path) == stamp:
            continue
        if os.path.basename(path) in NOT_CAMPAIGN_DATA:
            continue
        rel = os.path.relpath(path, campaign_root).replace(os.sep, "/")
        storage.upload_file(path, bucket, f"{prefix}{rel}")
        sent.append(rel)
    _publish_manifest(storage, bucket, prefix, campaign_root, sent)
    logger.info("Uploaded %d derived file(s) of campaign %s", len(sent), campaign_id)
    return len(sent)


def _publish_manifest(storage, bucket: str, prefix: str, campaign_root: str,
                      sent: list) -> None:
    """Record what was just uploaded, at :data:`~.postprocess_job.OUTPUT_MANIFEST`.

    Written even when nothing was sent. An empty manifest and a missing one mean different
    things to the fetch -- nothing was derived, against no Job ever said -- and only the
    first of those is a campaign that is fine.
    """
    from .postprocess_job import OUTPUT_MANIFEST  # noqa: PLC0415

    local = os.path.join(campaign_root, *OUTPUT_MANIFEST.split("/"))
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{rel}\n" for rel in sent))
    storage.upload_file(local, bucket, f"{prefix}{OUTPUT_MANIFEST}")


def _derive_batch(campaign_root: str, commands: list, force: bool) -> tuple:
    """Run one search batch's ``search.postprocessing`` commands, and nothing else.

    The same :func:`~robovast.results_processing.postprocessing.run_postprocessing_commands`
    the controller calls, against the same campaign root -- it is the function both
    postprocessing lists already share, so a batch loads its plugins and applies its
    execution contract exactly as the campaign-level pass does. What differs is only which
    list, and that the caller supplies it.

    Nothing here completes the campaign: this function has no ingest and no provenance
    record to skip, which is the reason a batch runs it rather than the campaign-level pass
    with steps turned off.
    """
    from robovast.common.config_plugins import ensure_plugins_importable  # noqa: PLC0415
    from robovast.common.results_utils import campaign_vast  # noqa: PLC0415
    from robovast.results_processing.postprocessing import \
        run_postprocessing_commands  # noqa: PLC0415

    # The campaign's own `plugins:` and any local `./path.py:Class` refs resolve against
    # its staged config, exactly as they do on the controller.
    config_dir = os.path.join(campaign_root, "_config")
    ensure_plugins_importable(campaign_root, vast_path=str(campaign_vast(campaign_root)))
    ok, _entries = run_postprocessing_commands(
        commands, results_dir=campaign_root, config_dir=config_dir,
        output=logger.info, force=force)
    return ok, ("batch derived" if ok else "a batch postprocessing step failed")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s")

    from robovast.client.logging_config import (  # noqa: PLC0415
        add_campaign_log_handler, remove_campaign_log_handler)

    from .postprocess_job import run_host_postprocessing  # noqa: PLC0415

    try:
        campaign_id = os.environ[ENV_CAMPAIGN_ID]
        dest = os.environ[ENV_STAGE_DEST]
        cluster_config = cluster_config_from_env()
    except (KeyError, ValueError) as e:
        print(f"Host postprocessing cannot start: {e}", file=sys.stderr)
        return 2

    force = os.environ.get(ENV_FORCE) == "1"
    skip = [s for s in (os.environ.get(ENV_SKIP) or "").split(",") if s.strip()]
    batch_commands = os.environ.get(ENV_COMMANDS)
    campaign_root = os.path.join(dest, campaign_id)

    # Appended to (the handler opens in mode "a"), never truncated: the conversion container
    # has already written its half of this file, and the two stages are one ordered
    # POSTPROCESSING section in the campaign log.
    log_path = os.path.join(campaign_root, "_execution", "postprocessing.log")
    handler = None
    try:
        handler = add_campaign_log_handler(log_path)
    except Exception:  # pylint: disable=broad-except
        logger.warning("Could not open %s; continuing without it.", log_path,
                       exc_info=True)

    before = _snapshot(campaign_root)
    ok, message, failure = False, "", None
    try:
        if batch_commands is None:
            # *results_dir* is the PARENT: the host stage takes a results directory and
            # names the campaign inside it, which is why staging lands the campaign one
            # level down.
            ok, message = run_host_postprocessing(
                dest, campaign_id, force=force, skip=skip)
        else:
            ok, message = _derive_batch(campaign_root, json.loads(batch_commands), force)
    except Exception as e:  # noqa: BLE001 - the upload below is the only record of this
        failure = e
        message = f"{type(e).__name__}: {e}"
    finally:
        # What this step cost, before the log handler closes so the figure lands in the
        # POSTPROCESSING section. Its memory peak is the peak up to *here* and so excludes
        # the upload that follows -- which is the right cut anyway: the ingest above is this
        # step's work, and the upload streams rather than accumulating.
        try:
            logger.info("%s", postprocess_usage.summary_line(
                postprocess_usage.record(campaign_root, "host")))
        except Exception:  # pylint: disable=broad-except
            logger.warning("Could not record what the host step used.", exc_info=True)
        # Before the upload, so the log file holds everything this stage logged.
        remove_campaign_log_handler(handler)
        try:
            _upload_derived(cluster_config, campaign_id, campaign_root, before)
        except Exception as e:  # noqa: BLE001 - a failed upload must not mask the failure
            print(f"Could not upload the postprocessing outputs: {e}", file=sys.stderr)

    if failure is not None or not ok:
        print(f"Host postprocessing failed: {message}", file=sys.stderr)
        return 1
    logger.info("%s of %s finished: %s",
                "Host postprocessing" if batch_commands is None else "Batch derivation",
                campaign_id, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
