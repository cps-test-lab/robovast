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

"""In-cluster rosbag→CSV conversion Job — the ROS2 half of analysis postprocessing.

Analysis postprocessing splits along a natural seam: only the ``rosbags_*`` → CSV
step needs ROS2; everything after it (``generate_data_db``, metadata) is plain
Python that runs wherever robovast is installed. Locally ``docker_exec.sh`` runs the
conversion in a container and *bind-mounts* the campaign dir, so outputs appear in
place. A pod cannot bind-mount the caller's filesystem, so in-cluster the conversion
runs as a **Job** and this module builds/creates/tracks it.

Two properties matter:

* **The Job runs the campaign's own execution image** (the system-under-test's
  image, recorded in ``<campaign>/_execution/execution.yaml``) — rosbags carry the
  SUT's *custom ROS2 message types* and only deserialize there. This mirrors local,
  which passes that same image to ``docker_exec.sh --image``.
* **Nothing is baked into that image.** The conversion scripts are *mounted in* via
  an initContainer + emptyDir (the K8s analog of ``-v $SCRIPT_DIR:/scripts:ro``),
  exactly as the run Jobs receive ``entrypoint.sh``/config through a volume. Inputs
  (``/bags``) and outputs (``/out``) are separate dirs — the run-Job pattern — so the
  Job uploads ``/out`` wholesale to a ``<campaign_prefix>_postproc/`` staging prefix
  without ever re-uploading a rosbag.
"""

import hashlib
import json
import logging
import re
import time

from .kubernetes_kueue import KUEUE_QUEUE_NAME

logger = logging.getLogger(__name__)

SIDECAR_IMAGE = "ghcr.io/cps-test-lab/robovast-sidecar:latest"
#: Staging prefix the Job mirrors its outputs to (campaign-relative layout preserved).
POSTPROC_PREFIX = "_postproc"
_POLL_SECONDS = 5
_DEFAULT_TIMEOUT = 3 * 60 * 60


def rosbag_commands_for(vast_path: str, skip=None, skip_rosout: bool = False) -> list:
    """The batched ``rosbags_process`` invocations a campaign's ``.vast`` asks for.

    Reuses the same batching the local path uses (``_batch_rosbags_commands`` merges
    every ``rosbags_*`` entry into one ``rosbags_process`` call per ``bag_dir``), so
    the Job runs exactly what ``vast results postprocess`` would. Returns a list of
    ``{plugins, bag_dir?, workers?}`` dicts — empty when the campaign configures no
    rosbag conversion (then no Job is needed).
    """
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        _batch_rosbags_commands, get_postprocessing_commands)

    commands = get_postprocessing_commands(vast_path)
    skip_set = set(skip or ())
    if skip_set:
        commands = [
            c for c in commands
            if (c if isinstance(c, str) else list(c.keys())[0]) not in skip_set
        ]
    out = []
    for cmd in _batch_rosbags_commands(commands, skip_rosout=skip_rosout):
        if isinstance(cmd, dict) and "rosbags_process" in cmd:
            out.append(cmd["rosbags_process"] or {})
    return out


def campaign_execution_image(campaign_dir) -> str:
    """The image the campaign's runs actually used (its ``_execution/execution.yaml``).

    This is the system-under-test's image — the only place its custom ROS2 message
    types deserialize — and the same source the local path feeds to
    ``docker_exec.sh --image``. Raises if it is absent rather than silently
    converting in the wrong image.
    """
    import os  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    path = os.path.join(str(campaign_dir), "_execution", "execution.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            image = (yaml.safe_load(f) or {}).get("image")
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"cannot read the campaign's execution image from {path}: {e}") from e
    if not image:
        raise ValueError(
            f"no execution image recorded in {path}; cannot pick the image whose "
            "custom ROS2 types the rosbags need")
    return str(image)


def sync_outputs(cluster_config, campaign_id: str, campaign_root: str) -> int:
    """Pull the Job's outputs (`_postproc/`) into *campaign_root*; return the count.

    Scoped to the staging prefix **on purpose**: ``download_prefix`` has no
    skip-existing, so mirroring the whole campaign prefix would re-download every
    rosbag. The Job mirrored its outputs at campaign-relative paths, so they land
    directly at ``<campaign_root>/<config>/<run>/``.
    """
    from robovast.execution.cluster_execution import in_pod_storage  # noqa: PLC0415

    bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
        cluster_config, campaign_id)
    storage = in_pod_storage.storage_client_for(cluster_config)
    n = storage.download_prefix(bucket, f"{campaign_prefix}{POSTPROC_PREFIX}", campaign_root)
    logger.info("Synced %d postprocessing output(s) into %s", n, campaign_root)
    return n


def campaign_vast(campaign_root) -> str:
    """The campaign's snapshotted ``.vast`` (``<campaign>/_config/*.vast``)."""
    from pathlib import Path  # noqa: PLC0415

    vasts = sorted(Path(campaign_root).joinpath("_config").glob("*.vast"))
    if not vasts:
        raise ValueError(f"no .vast under {campaign_root}/_config; cannot postprocess")
    return str(vasts[0])


def postprocess_campaign(cluster_config, campaign_id: str, campaign_root: str,
                         namespace: str, controller_image: str, force: bool = False,
                         skip=None, skip_rosout: bool = False) -> tuple:
    """Analysis postprocessing for one campaign, in-cluster. Returns ``(ok, message)``.

    The single implementation behind both entry points — the per-campaign controller
    (auto-chain) and the service (explicit re-run):

    1. **conversion Job** — rosbags→CSV in the campaign's own execution image (ROS2);
    2. **sync** its outputs (`_postproc/`) into *campaign_root* — done **regardless
       of the Job's outcome**, so a failed conversion's ``postprocessing.log`` (teed
       and mirrored even on failure) lands in the campaign log the web UI shows;
    3. **stage 2** — ``data.db`` + metadata, pure Python, right here (success only).

    *campaign_root* must already hold the campaign (`_config/`, `campaign.db`,
    `test.xml`s) — true for the controller (it built it) and for the service after
    ``fetch_campaign``.
    """
    rosbag_cmds = rosbag_commands_for(campaign_vast(campaign_root), skip=skip,
                                      skip_rosout=skip_rosout)
    if rosbag_cmds:
        image = campaign_execution_image(campaign_root)
        ok, message = run_conversion_job(
            cluster_config, campaign_id, namespace, image, rosbag_cmds,
            controller_image, force=force)
        # Sync the Job's outputs regardless of outcome. The conversion tees its
        # stdout/stderr to postprocessing.log and mirrors /out to the object store
        # even on failure, so this lands the POSTPROCESSING section (with the
        # conversion error) in the campaign log the web UI shows and finalize
        # uploads — without it, a failure surfaces only as a terse "kubectl logs"
        # hint the user cannot act on off-cluster.
        sync_outputs(cluster_config, campaign_id, campaign_root)
        if not ok:
            # Echo the conversion error to the service console too. The web UI already
            # has it via the synced postprocessing.log (POSTPROCESSING section); no
            # campaign log handler is attached at this point, so this reaches the
            # ``vast serve`` stdout only — not duplicated into the campaign log.
            import os  # noqa: PLC0415
            log_path = os.path.join(campaign_root, "_execution", "postprocessing.log")
            if os.path.isfile(log_path):
                with open(log_path, encoding="utf-8") as f:
                    logger.warning("Postprocessing conversion failed:\n%s",
                                   f.read().rstrip())
            return False, message
    else:
        logger.info("Campaign %s configures no rosbag conversion; host steps only",
                    campaign_id)
    import os  # noqa: PLC0415

    from robovast.common.logging_config import (  # noqa: PLC0415
        add_campaign_log_handler, remove_campaign_log_handler)

    # Append the pure-Python host stage's narrative to the same postprocessing.log
    # the conversion Job produced (mode "a"), so both stages form one ordered
    # POSTPROCESSING section. _finalize (auto-chain) / the re-run then upload it to
    # {prefix}_execution/postprocessing.log.
    log_path = os.path.join(str(campaign_root), "_execution", "postprocessing.log")
    handler = None
    try:
        handler = add_campaign_log_handler(log_path)
    except Exception:  # pylint: disable=broad-except
        logger.warning("Could not open postprocessing.log; continuing without it.",
                       exc_info=True)
    try:
        return run_host_postprocessing(
            os.path.dirname(str(campaign_root).rstrip(os.sep)),
            campaign_id, force=force, skip=skip)
    finally:
        remove_campaign_log_handler(handler)


def run_host_postprocessing(results_dir: str, campaign_id: str, force: bool = False,
                            skip=None) -> tuple:
    """Stage 2 — everything after the ROS conversion (``data.db``, metadata).

    Pure Python, so it runs wherever robovast is installed (the controller pod, the
    service pod). Reuses the *normal* pipeline with the rosbag steps skipped — the
    conversion Job already did those — so there is no second implementation of the
    postprocessing sequence. Returns ``(ok, message)``.
    """
    from robovast.results_processing.postprocessing import (  # noqa: PLC0415
        ROSBAG_BATCH_NAMES, run_postprocessing)

    return run_postprocessing(
        results_dir=results_dir, campaign=campaign_id, force=force,
        skip=sorted(set(skip or ()) | set(ROSBAG_BATCH_NAMES)),
        output_callback=logger.info)


#: This Job pod runs in a separate context from the controller, so its stdout is
#: otherwise only a transient ``kubectl logs``. Teeing the conversion output to this
#: campaign-relative path lets it ride the wholesale ``/out`` mirror below into the
#: object store, where :func:`sync_outputs` lands it at
#: ``<campaign_root>/_execution/postprocessing.log`` — the POSTPROCESSING section of
#: the unified campaign log (the host stage then appends to the same file).
_POSTPROC_LOG = "/out/_execution/postprocessing.log"


def _conversion_script(rosbag_cmds: list, force: bool) -> str:
    """The main container's shell: convert each batch, then mirror /out up.

    All conversion stdout/stderr is teed into ``_POSTPROC_LOG`` so it becomes the
    POSTPROCESSING section of the campaign's unified log. ``pipefail`` preserves the
    conversion's exit status through the ``tee`` pipe, and the ``/out`` mirror runs
    unconditionally so the log (with any error) is uploaded even on failure.
    """
    convert = ["set -e"]
    for params in rosbag_cmds:
        args = [
            "/scripts/ros2_exec.sh", "/scripts/rosbags_process.py",
            "--config", _shquote(json.dumps({"plugins": params.get("plugins", [])})),
            # Outputs go to their own tree (never beside the bags), so the upload
            # below carries only what this Job produced.
            "--output-root", "/out",
        ]
        if params.get("bag_dir") is not None:
            args += ["--bag-dir", _shquote(str(params["bag_dir"]))]
        if params.get("workers") is not None:
            args += ["--workers", str(int(params["workers"]))]
        if force:
            args.append("--force")
        args.append("/bags")
        convert.append(" ".join(args))

    lines = [
        "set -eo pipefail",
        f"mkdir -p $(dirname {_POSTPROC_LOG})",
        '/tools/mc alias set mystore "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY"',
        "rc=0",
        # Run the conversions in a subshell, teeing their combined output to the log.
        "( " + "\n".join(convert) + f'\n ) 2>&1 | tee -a "{_POSTPROC_LOG}" || rc=$?',
        # Wholesale upload of the output tree — no diffing needed (inputs live in
        # /bags). Unconditional so the log rides up even when a conversion failed.
        '/tools/mc mirror --overwrite /out/ '
        f'"mystore/$S3_BUCKET/${{S3_CAMPAIGN_PREFIX}}{POSTPROC_PREFIX}/"',
        "exit $rc",
    ]
    return "\n".join(lines)


def _shquote(value: str) -> str:
    import shlex  # noqa: PLC0415
    return shlex.quote(value)


def _short_job_name(prefix: str, campaign: str) -> str:
    """Build a Kubernetes Job name ``<prefix><campaign>`` capped at 63 chars.

    Kubernetes copies the Job's ``metadata.name`` verbatim into the pod template's
    ``job-name`` label, and label values may be at most 63 chars — so the *name*
    itself (not just the label-safe campaign) has to fit, otherwise the Job is
    rejected with ``spec.template.labels: ... must be no more than 63 characters``.
    Keep the readable head of the campaign and append a short hash so distinct
    campaigns that share a truncated head still map to distinct Job names.
    """
    safe = re.sub(r"[^a-z0-9.-]", "", campaign.lower().replace("_", "-"))
    full = f"{prefix}{safe}"
    if len(full) <= 63:
        return full
    digest = hashlib.sha256(campaign.encode()).hexdigest()[:8]
    head = safe[: 63 - len(prefix) - 1 - len(digest)].rstrip("-.")
    return f"{prefix}{head}-{digest}"


def build_manifest(campaign_id: str, image: str, rosbag_cmds: list, s3: tuple,
                   namespace: str, controller_image: str, force: bool = False) -> dict:
    """Build the conversion Job manifest.

    Args:
        campaign_id: The campaign to convert.
        image: **The campaign's execution image** (the SUT image from
            ``_execution/execution.yaml``) — required for its custom ROS2 types.
        rosbag_cmds: :func:`rosbag_commands_for` output.
        s3: ``(endpoint, access_key, secret_key, bucket, campaign_prefix)``.
        namespace: Kubernetes namespace.
        controller_image: An image that has the robovast package — used only by an
            initContainer to *copy the conversion scripts in*.
        force: Bypass the per-rosbag caches.
    """
    from robovast.execution.cluster_execution.cluster_execution import (  # noqa: PLC0415
        _label_safe_campaign)

    endpoint, access_key, secret_key, bucket, campaign_prefix = s3
    safe = _label_safe_campaign(campaign_id)
    s3_env = [
        {"name": "S3_ENDPOINT", "value": endpoint},
        {"name": "S3_BUCKET", "value": bucket},
        {"name": "S3_ACCESS_KEY", "value": access_key},
        {"name": "S3_SECRET_KEY", "value": secret_key},
        {"name": "S3_CAMPAIGN_PREFIX", "value": campaign_prefix},
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": _short_job_name("robovast-postproc-", campaign_id),
            "namespace": namespace,
            "labels": {
                "jobgroup": "postprocessing",
                "campaign-id": safe,
                # Kueue keys queue membership off the label, not an annotation.
                "kueue.x-k8s.io/queue-name": KUEUE_QUEUE_NAME,
            },
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {
                    "labels": {"jobgroup": "postprocessing", "campaign-id": safe},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "volumes": [
                        {"name": "scripts", "emptyDir": {}},
                        {"name": "tools", "emptyDir": {}},
                        {"name": "bags", "emptyDir": {}},
                        {"name": "out", "emptyDir": {}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                    "initContainers": [
                        {
                            # Mount the conversion scripts in — never baked into the
                            # SUT image (the K8s analog of docker_exec.sh's -v).
                            "name": "scripts",
                            "image": controller_image,
                            "command": ["python", "-c",
                                        "import os,shutil,robovast.results_processing.data as d;"
                                        "shutil.copytree(os.path.dirname(d.__file__),'/scripts',"
                                        "dirs_exist_ok=True)"],
                            "volumeMounts": [{"name": "scripts", "mountPath": "/scripts"}],
                        },
                        {
                            # mc for the upload + mirror the campaign (rosbags) down.
                            "name": "s3-init",
                            "image": SIDECAR_IMAGE,
                            "command": ["sh", "-c",
                                        'cp "$(command -v mc)" /tools/mc && '
                                        'chmod +x /tools/mc /scripts/*.sh 2>/dev/null; '
                                        'mc alias set mystore "$S3_ENDPOINT" "$S3_ACCESS_KEY" '
                                        '"$S3_SECRET_KEY" && '
                                        'mc mirror "mystore/$S3_BUCKET/$S3_CAMPAIGN_PREFIX" /bags/'],
                            "env": s3_env,
                            "volumeMounts": [
                                {"name": "tools", "mountPath": "/tools"},
                                {"name": "scripts", "mountPath": "/scripts"},
                                {"name": "bags", "mountPath": "/bags"},
                            ],
                        },
                    ],
                    "containers": [{
                        "name": "convert",
                        # The system-under-test's own image: custom ROS2 types only
                        # deserialize here. ros2_exec.sh sources /opt/ros + /ws/install.
                        "image": image,
                        "command": ["/bin/bash", "-c", _conversion_script(rosbag_cmds, force)],
                        "env": s3_env,
                        "volumeMounts": [
                            {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
                            {"name": "tools", "mountPath": "/tools", "readOnly": True},
                            {"name": "bags", "mountPath": "/bags"},
                            {"name": "out", "mountPath": "/out"},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                    }],
                },
            },
        },
    }


def run_conversion_job(cluster_config, campaign_id: str, namespace: str, image: str,
                       rosbag_cmds: list, controller_image: str, force: bool = False,
                       timeout: int = _DEFAULT_TIMEOUT) -> tuple:
    """Create the conversion Job and wait for it. Returns ``(ok, message)``.

    A no-op success when the campaign configures no rosbag conversion.
    """
    if not rosbag_cmds:
        return True, "no rosbag conversion configured; nothing to run"

    from kubernetes import client  # noqa: PLC0415
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    from robovast.execution.cluster_execution import in_pod_storage  # noqa: PLC0415

    bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
        cluster_config, campaign_id)
    access_key, secret_key = cluster_config.get_s3_credentials()
    s3 = (cluster_config.get_s3_endpoint(), access_key, secret_key, bucket, campaign_prefix)

    manifest = build_manifest(campaign_id, image, rosbag_cmds, s3, namespace,
                              controller_image, force=force)
    name = manifest["metadata"]["name"]
    batch = client.BatchV1Api()
    try:
        batch.create_namespaced_job(namespace=namespace, body=manifest)
    except ApiException as e:
        if e.status != 409:  # already exists → fall through and wait on it
            return False, f"could not create postprocessing job: {e}"
    logger.info("Postprocessing job %s created (image=%s)", name, image)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = batch.read_namespaced_job_status(name=name, namespace=namespace).status
        except ApiException as e:
            if e.status == 404:  # reaped (ttl) — treat as finished
                return True, "postprocessing job finished (reaped)"
            return False, f"could not read postprocessing job status: {e}"
        if not status.active:
            if status.succeeded:
                logger.info("Postprocessing job %s succeeded", name)
                return True, "rosbag conversion complete"
            if status.failed:
                return False, (f"postprocessing job {name} failed — see the "
                               f"POSTPROCESSING section of the campaign log for the "
                               f"conversion error (kubectl logs job/{name} -n {namespace})")
        time.sleep(_POLL_SECONDS)
    return False, f"postprocessing job {name} timed out after {timeout}s"
