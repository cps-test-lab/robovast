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
"""Stage one campaign's data from the object store into the postprocessing pod.

Run as the postprocessing Job's staging initContainer::

    python3 -m robovast.execution.cluster_execution.postprocess_stage

**Why a Python stage and not a mirror.** ``StorageClient.download_prefix`` fetches one
object at a time into a bounded buffer, so its memory footprint is a function of the
largest object and not of how many there are; a whole-prefix mirror grows with the object
count and a campaign holds tens of thousands, which is a container limit that has to rise
with every campaign size. It also reports as it goes: staging a campaign takes minutes, and
this is the only channel that shows those minutes progressing rather than a silent
container that either finishes or is killed.

The campaign lands at ``<ROBOVAST_STAGE_DEST>/<campaign_id>``, one level below the
destination, because the host stage takes the **parent** as its results dir and names the
campaign inside it (see :func:`postprocess_job.run_host_postprocessing`).
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

#: Campaign to stage. Required -- there is no default campaign.
ENV_CAMPAIGN_ID = "ROBOVAST_CAMPAIGN_ID"

#: Parent directory the campaign is staged under. Required rather than defaulted: it is a
#: mount point the Job manifest owns, and guessing it would write into the image.
ENV_STAGE_DEST = "ROBOVAST_STAGE_DEST"

#: ``"1"`` also excludes the rosbag directories. Set when no rosbag conversion is
#: configured: the bags are then the bulk of the transfer and nothing in the pod opens them.
ENV_SKIP_BAGS = "ROBOVAST_STAGE_SKIP_BAGS"

#: Directory names holding rosbags, excluded when :data:`ENV_SKIP_BAGS` is set. These are
#: the ``bag_dir`` values ``robovast.results_processing.postprocessing``'s rosbag batch map
#: defaults to (``rosbag2`` for the per-run bag, ``logs/rosout_bag`` for the infrastructure
#: one); the map is private, so they are named here and matched as path segments, which
#: covers ``logs/rosout_bag`` without depending on where under the run it sits.
BAG_DIR_NAMES = ("rosbag2", "rosout_bag")

#: Exit code -> what it means, so a caller reading the pod's status can say which failure it
#: was without parsing a log that a failing stage may never have written.
#:
#: Distinct codes rather than a bare 1: an exit code is the one channel that survives every
#: staging failure, including the one where the object store is exactly what cannot be
#: reached, and ``exited 1`` is every failure at once.
STAGE_EXIT_REASONS: dict[int, str] = {
    41: "the staging configuration or environment is unusable",
    42: "the object store could not be reached",
    43: "staging could not write to local disk (out of space?)",
}

#: How often a progress line is emitted, in seconds. Frequent enough that a stalled stage is
#: visibly stalled, rare enough that the container log does not become the transfer's cost.
_PROGRESS_INTERVAL = 15


def cluster_config_from_env():
    """Rebuild the cluster config the deployed service was set up with.

    The service's Deployment env carries the config plugin name and the setup kwargs it was
    created with, and every pod it launches inherits them, so this is a read of the
    authoritative record rather than a second source of truth for storage access. Raises
    ``ValueError`` when the name is absent -- a pod that cannot name its cluster config
    cannot reach the store, and staging on a guess would silently target the wrong bucket.
    """
    import json  # noqa: PLC0415

    from .cluster_setup import get_cluster_config  # noqa: PLC0415

    name = os.environ.get("ROBOVAST_CLUSTER_CONFIG_NAME")
    if not name:
        raise ValueError(
            "cluster config not configured (ROBOVAST_CLUSTER_CONFIG_NAME); this pod must "
            "be launched by a service deployed with 'vast cluster setup'")
    cfg = get_cluster_config(name)
    raw = os.environ.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
    kwargs = json.loads(raw) if raw else {}
    if kwargs:
        cfg.restore_from_setup_kwargs(kwargs)
    return cfg


#: The phase file this pod is about to write, which it must not be handed a copy of.
#:
#: The conversion APPENDS to it, so a previous attempt's copy would become the head of this
#: attempt's log -- and did: a postprocess whose conversion failed showed, as its account,
#: the image-pull failure of the attempt before it. It is also the file the Job's log is
#: published to while it runs, so staging it back would fold this attempt's own head into
#: itself.
NOT_STAGED_LOG = "_execution/postprocessing.log"

def not_staged_sections() -> str:
    """The prefix holding finished sections of earlier runs of a repeatable phase.

    Not staged into this pod either. They are the immutable history of the campaign log,
    read only by whoever streams it, and nothing here produces or consumes one -- so
    staging them would transfer bytes the pod cannot use and hand the tail upload a second
    copy to publish.

    A function rather than a constant because it needs ``robovast.common``, and this
    module's ``robovast`` imports are deliberately below module scope: an unimportable
    package must exit with a diagnosis rather than a traceback from the interpreter.
    """
    from robovast.common.campaign_logs import SECTIONS_DIR  # noqa: PLC0415
    return f"_execution/{SECTIONS_DIR}/"


def build_include(skip_bags: bool):
    """Return the ``download_prefix`` predicate deciding what a pod is given.

    Called with each object's key **relative to the campaign prefix**, so it matches the
    campaign-relative layout directly.

    The probe directory is excluded unconditionally. A calibration probe is deliberately not
    a run, so its bag is not campaign data: converting it costs a bag's work per node, and
    an interrupted probe's unfinalized bag fails a step on something nothing reads. Deciding
    it here rather than in a skip list is what keeps it decided once -- what the pod never
    receives it cannot convert, cannot fail on, and does not pay to download.

    Only the probe directory, never the reserved directories as a set: the others hold data
    the pod needs, and ``_jobs/<batch>/<job>/logs/rosout_bag`` is each job's real log bag, so
    excluding them wholesale would drop every ``/rosout`` record in the campaign.

    The two log exclusions are :data:`NOT_STAGED_LOG` and :func:`not_staged_sections`.
    """
    from robovast.common.campaign_data import PROBE_DIR  # noqa: PLC0415
    sections_prefix = not_staged_sections()

    def include(rel: str) -> bool:
        parts = rel.split("/")
        if parts[0] == PROBE_DIR:
            return False
        if rel == NOT_STAGED_LOG or rel.startswith(sections_prefix):
            return False
        if skip_bags and any(p in BAG_DIR_NAMES for p in parts[:-1]):
            return False
        return True

    return include


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s")

    from robovast.common.errors import ObjectStoreUnreachableError  # noqa: PLC0415

    from . import in_pod_storage  # noqa: PLC0415

    try:
        campaign_id = os.environ[ENV_CAMPAIGN_ID]
        dest = os.environ[ENV_STAGE_DEST]
        if not campaign_id or not dest:
            raise ValueError(f"{ENV_CAMPAIGN_ID} and {ENV_STAGE_DEST} must be non-empty")
        skip_bags = os.environ.get(ENV_SKIP_BAGS) == "1"
        cluster_config = cluster_config_from_env()
        bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
            cluster_config, campaign_id)
        storage = in_pod_storage.storage_client_for(cluster_config)
    except (KeyError, ValueError, ImportError) as e:
        print(f"Staging cannot start: {e}", file=sys.stderr)
        return 41

    campaign_root = os.path.join(dest, campaign_id)
    logger.info("Staging campaign %s into %s (bags %s)", campaign_id, campaign_root,
                "excluded" if skip_bags else "included")

    def report(done, total, bytes_done, bytes_total):
        logger.info("Staged %s/%s file(s), %.1f/%.1f MiB", done, total,
                    bytes_done / (1 << 20), (bytes_total or 0) / (1 << 20))

    try:
        count = storage.download_prefix(
            bucket, campaign_prefix, campaign_root,
            include=build_include(skip_bags),
            # The tree is read, never executed, so restoring per-file executable bits would
            # buy nothing for one metadata round-trip per file -- the transfer's dominant
            # cost at this file count.
            executable_bits=False,
            on_progress=in_pod_storage.download_progress_reporter(
                report, interval=_PROGRESS_INTERVAL))
    except ObjectStoreUnreachableError as e:
        print(f"Staging could not reach the object store: {e}", file=sys.stderr)
        return 42
    except OSError as e:
        print(f"Staging could not write to {campaign_root}: {e}", file=sys.stderr)
        return 43
    except Exception as e:  # noqa: BLE001 - the code is the channel; say what it was
        print(f"Staging failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    logger.info("Staged %d file(s) of campaign %s into %s", count, campaign_id,
                campaign_root)

    # A campaign has every file in the store and none of its links: a symlink is not an
    # object, so `<config>/<run>/job` -- the user-facing way into a run's job artifacts --
    # cannot survive the round trip. The link manifest can, and this is what it is for, so
    # the tree is completed rather than each reader taught to cope: metadata generation
    # reads sysinfo.yaml through that link and failed with "sysinfo.yaml not found" on a
    # campaign whose sysinfo.yaml had been staged the whole time.
    try:
        from robovast.common.execution import create_job_links  # noqa: PLC0415
        links = create_job_links(campaign_root)
    except OSError as e:
        # Not fatal and not silent: a reader that goes through the manifest is unaffected,
        # and one that goes through the link says which file it could not find.
        logger.warning("Could not restore the job links of %s: %s", campaign_id, e)
    else:
        logger.info("Restored %d job link(s) of campaign %s", links, campaign_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
