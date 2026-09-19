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

Run as the postprocessing Job's main container, after the ``stage`` initContainer has
landed the campaign (and after the optional conversion container has produced its CSVs)::

    python3 -m robovast.execution.cluster_execution.postprocess_host

It runs beside the data instead of fetching it: the same
:func:`postprocess_job.run_host_postprocessing` the off-cluster lane calls, so there is one
implementation of the sequence, but with the campaign already on local disk. What it derives
then has to be sent back, because this pod's filesystem does not outlive it: one tar of what
the Job produced, streamed as a ``PUT`` to the service's data plane, which writes it into
the campaign's directory on the results volume.
"""

import json
import logging
import os
import sys
import tarfile
import threading
import time

from . import pod_access, postprocess_usage

logger = logging.getLogger(__name__)

#: Parent directory the campaign was staged under; the campaign is one level below it, named
#: by ``ROBOVAST_CAMPAIGN_ID``. Required rather than defaulted: it is a mount point the Job
#: manifest owns, and guessing it would read the image.
ENV_STAGE_DEST = "ROBOVAST_STAGE_DEST"

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
#: written back into its directory on the service.
#:
#: The stat-diff below treats anything new as derived output, which is right for everything
#: the stages produce and wrong for a scratch file. ``rosbags_process`` keeps its per-bag
#: hash cache beside the bag it describes, and that path is fixed in the script with no
#: override -- so it is filtered here rather than relocated. It is rebuildable by definition
#: and describes a pod that no longer exists, so delivering it would add a file per bag to
#: every campaign for a cache no later reader can use.
NOT_CAMPAIGN_DATA = frozenset({".robovast_rosbags_process_cache"})

#: How many times the delivery is attempted, and how long between attempts. A streamed body
#: cannot be replayed by the client library, so a retry is the whole pipeline again -- and
#: worth it, because the one failure this meets in practice is the service being rolled
#: while a long postprocess runs, which is over in seconds.
_DELIVERY_ATTEMPTS = 5
_DELIVERY_RETRY_S = 3.0


def _snapshot(root: str) -> dict:
    """Map every file under *root* to ``(size, mtime_ns)``.

    Taken before the host stage runs, so a file that appears or changes afterwards is this
    stage's output. That is what keeps the delivery proportional to what was produced rather
    than to the campaign that was staged.

    It cannot identify the CONVERSION's output, though: that container finishes before this
    one starts, so its CSVs are already on disk when this is taken and read as staged data.
    :func:`_conversion_outputs` is the other half of the answer, and the two are what
    :func:`_upload_derived` delivers.

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


def _conversion_outputs(campaign_root: str) -> set:
    """Campaign-relative paths the conversion container derived from the bags.

    These have to be named rather than diffed. The conversion runs as an initContainer, so
    it has already written its CSVs by the time this container takes its snapshot -- while
    the service has never held them, because that container carries no token and delivers
    nothing. Diffed alone, every one of them reads as staged data and stays in a pod that is
    about to be deleted: ``poses.csv``, the per-action feedback and status tables, the
    costmaps and the raw behaviour-tree transitions would be ingested into the index and then
    dropped, so a campaign's own download held none of the tables its analysis reads.

    Read from the record the conversion writes for this purpose, at
    :data:`~robovast.results_processing.postprocessing.STAGED_PROVENANCE`, whose ``output``
    paths are relative to the campaign root -- the same base the delivery keys on. Empty
    when there was no conversion (a campaign with no bags, or a per-batch Job), which is a
    campaign for which the diff alone is the whole answer.
    """
    from robovast.results_processing.postprocessing import \
        _staged_provenance_entries  # noqa: PLC0415

    return {entry["output"] for entry in _staged_provenance_entries(campaign_root)
            if entry.get("output")}


def _never_sent() -> tuple:
    """``(basenames, campaign-relative paths)`` the data plane refuses, kept out here.

    The same two lists the service's writing half refuses on
    (:data:`~robovast.service.tar_io.DENY_ALWAYS`,
    :data:`~robovast.service.data_app.DRIVER_OWNED`), imported rather than repeated so the
    pod and the service cannot disagree about them. Filtered before the tar rather than
    left to the server: a refused member is reported there as something that should not
    have been sent, and neither can appear in what this Job derived -- the campaign's own
    store is the driver's, and so are the driver's logs.
    """
    from robovast.service.data_app import DRIVER_OWNED  # noqa: PLC0415
    from robovast.service.tar_io import DENY_ALWAYS  # noqa: PLC0415

    return frozenset(DENY_ALWAYS) | NOT_CAMPAIGN_DATA, frozenset(DRIVER_OWNED)


def derived_paths(campaign_root: str, before: dict) -> list:
    """Campaign-relative paths of what this Job derived, sorted.

    Everything that differs from the snapshot **or** the conversion named it
    (:func:`_conversion_outputs`) -- those two together being what this Job derived, and
    nothing else, so the staged run data is not written back over itself. ``_execution/``
    is under the same rule and no other: the diff carries ``postprocessing.log``, the
    conversion's provenance and the usage record, which this Job wrote, and leaves
    ``controller.log`` and the other driver-owned files alone -- a staged snapshot of the
    driver's log landing on the service would truncate the record to the moment the pod
    was given its copy.
    """
    denied_names, denied_paths = _never_sent()
    converted = _conversion_outputs(campaign_root)
    out = []
    for path, stamp in _snapshot(campaign_root).items():
        rel = os.path.relpath(path, campaign_root).replace(os.sep, "/")
        if before.get(path) == stamp and rel not in converted:
            continue
        if os.path.basename(path) in denied_names or rel in denied_paths:
            continue
        out.append(rel)
    return sorted(out)


def _tar_body(campaign_root: str, rels: list):
    """A generator of plain-tar bytes carrying *rels* relative to *campaign_root*.

    A writer thread tars into one end of a pipe and the generator reads the other, so the
    body streams as it is made and nothing the size of the outputs sits in memory or on the
    pod's disk. Regular files only, by construction of :func:`derived_paths`; a file that
    vanished between the diff and the read costs one member, not the delivery.
    """
    read_fd, write_fd = os.pipe()
    failure: list = []

    def _write():
        try:
            with os.fdopen(write_fd, "wb") as sink, \
                    tarfile.open(fileobj=sink, mode="w|") as tar:
                for rel in rels:
                    try:
                        tar.add(os.path.join(campaign_root, rel), arcname=rel,
                                recursive=False)
                    except OSError as e:
                        logger.warning("Not delivered, it changed under the tar: %s (%s)",
                                       rel, e)
        except BaseException as e:  # noqa: BLE001 - re-raised by the reader
            # The `with` has closed the pipe's write end, so the reader sees EOF and
            # finishes; a reader that went away first shows up here as a broken pipe.
            failure.append(e)

    thread = threading.Thread(target=_write, name="postprocess-deliver", daemon=True)
    thread.start()
    try:
        with os.fdopen(read_fd, "rb") as source:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                yield chunk
    finally:
        # The read end is closed by now (the `with` above), so a writer still blocked on
        # a full pipe -- the consumer stopped early -- fails on its next write and ends.
        thread.join()
    if failure:
        raise failure[0]


def _deliver(campaign_root: str, rels: list, data_url: str, token: str,
             campaign_id: str) -> None:
    """``PUT`` the tar of *rels* to the campaign's outputs route; raise if it never landed.

    Retried whole on a transient failure -- a connection refused or reset, a 5xx -- because
    a streamed body cannot be replayed and the service being rolled mid-postprocess is
    exactly what these meet. A 4xx is not retried: the route refused what was sent, and
    sending it again changes nothing.
    """
    import requests  # noqa: PLC0415

    from robovast.service.interface import Routes  # noqa: PLC0415

    url = data_url.rstrip("/") + Routes.campaign_outputs(campaign_id)[len(Routes.DATA):]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/x-tar"}
    last = None
    for attempt in range(1, _DELIVERY_ATTEMPTS + 1):
        try:
            response = requests.put(url, data=_tar_body(campaign_root, rels),
                                    headers=headers, timeout=(10, 600))
        except requests.RequestException as e:
            last = e
            logger.warning("Delivery attempt %d/%d could not reach the data plane: %s",
                           attempt, _DELIVERY_ATTEMPTS, e)
        else:
            if response.status_code < 400:
                ingested = response.json() if response.content else {}
                refused = ingested.get("refused") or []
                if refused:
                    logger.warning("The data plane refused %d member(s): %s", len(refused),
                                   ", ".join(refused[:5]))
                return
            if response.status_code < 500:
                raise RuntimeError(f"the data plane refused the outputs "
                                   f"({response.status_code}): {response.text[:500]}")
            last = RuntimeError(f"{response.status_code}: {response.text[:200]}")
            logger.warning("Delivery attempt %d/%d failed on the service side: %s",
                           attempt, _DELIVERY_ATTEMPTS, last)
        if attempt < _DELIVERY_ATTEMPTS:
            time.sleep(_DELIVERY_RETRY_S)
    raise RuntimeError(f"could not deliver the outputs after {_DELIVERY_ATTEMPTS} "
                       f"attempts: {last}")


def _upload_derived(campaign_root: str, before: dict, data_url: str, token: str,
                    campaign_id: str) -> int:
    """Send this Job's outputs to the campaign on the service; return how many.

    What goes is :func:`derived_paths`: the stat-diff against *before* plus the
    conversion's declared outputs, less what is never the campaign's. Delivered as one tar
    to the outputs route, where the last writer wins per member -- which is what lets a
    re-run replace a table it derived before.

    Nothing to send is not a failure: a batch that derived no rows, or a host pass whose
    every step was cached, has left the campaign as it was.
    """
    rels = derived_paths(campaign_root, before)
    if not rels:
        logger.info("Nothing derived for campaign %s to deliver", campaign_id)
        return 0
    _deliver(campaign_root, rels, data_url, token, campaign_id)
    logger.info("Delivered %d derived file(s) of campaign %s", len(rels), campaign_id)
    return len(rels)


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


def _required(name: str) -> str:
    """The non-empty value of environment variable *name*, or a ``KeyError`` naming it."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise KeyError(name)
    return value


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s")

    from robovast.client.logging_config import (  # noqa: PLC0415
        add_campaign_log_handler, remove_campaign_log_handler)

    from .postprocess_job import run_host_postprocessing  # noqa: PLC0415

    try:
        campaign_id = _required(pod_access.CAMPAIGN_ID_ENV)
        dest = _required(ENV_STAGE_DEST)
        data_url = _required(pod_access.DATA_URL_ENV)
        token = _required(pod_access.TOKEN_ENV)
    except KeyError as e:
        print(f"Host postprocessing cannot start: {e.args[0]} is not set", file=sys.stderr)
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
    except Exception as e:  # noqa: BLE001 - the delivery below is the only record of this
        failure = e
        message = f"{type(e).__name__}: {e}"
    finally:
        # What this step cost, before the log handler closes so the figure lands in the
        # POSTPROCESSING section. Its memory peak is the peak up to *here* and so excludes
        # the delivery that follows -- which is the right cut anyway: the ingest above is
        # this step's work, and the delivery streams rather than accumulating.
        try:
            logger.info("%s", postprocess_usage.summary_line(
                postprocess_usage.record(campaign_root, "host")))
        except Exception:  # pylint: disable=broad-except
            logger.warning("Could not record what the host step used.", exc_info=True)
        # Before the delivery, so the log file holds everything this stage logged.
        remove_campaign_log_handler(handler)
        try:
            _upload_derived(campaign_root, before, data_url, token, campaign_id)
        except Exception as e:  # noqa: BLE001 - a failed delivery must not mask the failure
            print(f"Could not deliver the postprocessing outputs: {e}", file=sys.stderr)
            if failure is None and ok:
                # The work succeeded and its results are in a pod about to be deleted:
                # that is a failed postprocess, not a successful one with a warning.
                ok, message = False, f"the outputs could not be delivered: {e}"

    if failure is not None or not ok:
        print(f"Host postprocessing failed: {message}", file=sys.stderr)
        return 1
    logger.info("%s of %s finished: %s",
                "Host postprocessing" if batch_commands is None else "Batch derivation",
                campaign_id, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
