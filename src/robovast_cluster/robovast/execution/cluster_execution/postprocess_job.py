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

from robovast.common.execution import resolve_sidecar_image

from .kube_client import api_transport_errors

logger = logging.getLogger(__name__)

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
    for cmd in _batch_rosbags_commands(commands, skip_rosout=skip_rosout, skip=skip_set):
        if isinstance(cmd, dict) and "rosbags_process" in cmd:
            out.append(cmd["rosbags_process"] or {})
    return out


def campaign_execution_image(campaign_dir) -> str:
    """The image the campaign's runs actually used (its ``_execution/execution.yaml``).

    This is the system-under-test's image — the only place its custom ROS2 message
    types deserialize — and the same source the local path feeds to
    ``docker_exec.sh --image``. Prefers the pinned ``image_revision`` when it is an
    immutable ``repo@sha256:…`` digest (recorded at run time, see
    ``create_execution_yaml`` / ``KubernetesBackend._capture_image_digest``) so a
    re-postprocess deserializes bags against the *exact* image the runs recorded them
    with, not whatever a floating ``:latest`` resolves to now. Falls back to the
    ``image`` tag. Raises if neither is present rather than converting in the wrong image.
    """
    import os  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    path = os.path.join(str(campaign_dir), "_execution", "execution.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"cannot read the campaign's execution image from {path}: {e}") from e
    revision = data.get("image_revision")
    if isinstance(revision, str) and "@sha256:" in revision:
        return revision
    image = data.get("image")
    if not image:
        raise ValueError(
            f"no execution image recorded in {path}; cannot pick the image whose "
            "custom ROS2 types the rosbags need")
    return str(image)


def sync_outputs(cluster_config, campaign_id: str, campaign_root: str,
                 force: bool = False) -> int:
    """Pull the Job's outputs (`_postproc/`) into *campaign_root*; return the count.

    Scoped to the staging prefix **on purpose**: mirroring the whole campaign prefix
    would walk every rosbag to find the handful of CSVs beside them. The Job mirrored
    its outputs at campaign-relative paths, so they land directly at
    ``<campaign_root>/<config>/<run>/``.

    *force* must be set whenever the conversion **replaced** outputs rather than adding
    them, i.e. whenever it ran with the caches bypassed. ``download_prefix`` skips a local
    file whose size already matches -- correct for the immutable durable home, wrong for a
    re-postprocess that mutates objects in place, where a regenerated CSV that keeps its
    byte count would be skipped and the campaign root would keep the file the user asked
    to replace.

    Left off, the skip is what makes the search loop's per-batch call cheap: the staging
    prefix only grows, and each batch re-lists it to fetch the few objects its own
    conversion added.
    """
    from . import in_pod_storage  # noqa: PLC0415

    bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
        cluster_config, campaign_id)
    storage = in_pod_storage.storage_client_for(cluster_config)
    n = storage.download_prefix(bucket, f"{campaign_prefix}{POSTPROC_PREFIX}", campaign_root,
                                force=force)
    logger.info("Synced %d postprocessing output(s) into %s", n, campaign_root)
    return n


def campaign_vast(campaign_root) -> str:
    """The campaign's ``.vast`` (``<campaign>/_config/<name>.vast``) — the same single
    source of truth the service edits in place, so the cluster conversion Job runs
    exactly the config the re-run dialog saved.
    """
    from robovast.common.results_utils import campaign_vast as _campaign_vast  # noqa: PLC0415

    return str(_campaign_vast(campaign_root))


def postprocess_campaign(cluster_config, campaign_id: str, campaign_root: str,
                         namespace: str, force: bool = False,
                         skip=None, skip_rosout: bool = False,
                         kube_context=None, state=None) -> tuple:
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

    *kube_context* must be the same context the campaign's Jobs were submitted with;
    ``None`` means the active kubeconfig context, which is only correct when the caller
    has none of its own.

    *state*, when the caller has one, is where stage 2's step lines are published as the live
    ``stage`` marker (see
    :func:`~robovast.execution.control_server.stage_output_callback`). Stage 1 is deliberately
    **not** covered: it runs in a pod, so its progress reaches this process only when the Job
    ends and its log is synced — the marker therefore stays empty for the conversion and
    starts moving at ``[1/n]``. Reporting stage 1 means tailing the Job's log from
    :func:`run_conversion_job`'s existing poll, which is a separate change.
    """
    rosbag_cmds = rosbag_commands_for(campaign_vast(campaign_root), skip=skip,
                                      skip_rosout=skip_rosout)
    if rosbag_cmds:
        image = campaign_execution_image(campaign_root)
        ok, message = run_conversion_job(
            cluster_config, campaign_id, namespace, image, rosbag_cmds, force=force,
            kube_context=kube_context)
        # Sync the Job's outputs regardless of outcome. The conversion tees its
        # stdout/stderr to postprocessing.log and mirrors /out to the object store
        # even on failure, so this lands the POSTPROCESSING section (with the
        # conversion error) in the campaign log the web UI shows and finalize
        # uploads — without it, a failure surfaces only as a terse "kubectl logs"
        # hint the user cannot act on off-cluster.
        # force rides along: it made the Job bypass its caches and REPLACE the CSVs, and
        # the fetch skips same-size files unless told not to.
        sync_outputs(cluster_config, campaign_id, campaign_root, force=force)
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

    from robovast.client.logging_config import add_campaign_log_handler  # noqa: PLC0415
    from robovast.client.logging_config import remove_campaign_log_handler

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
            campaign_id, force=force, skip=skip, state=state)
    finally:
        remove_campaign_log_handler(handler)


def run_host_postprocessing(results_dir: str, campaign_id: str, force: bool = False,
                            skip=None, state=None) -> tuple:
    """Stage 2 — everything after the ROS conversion (``data.db``, metadata).

    Pure Python, so it runs wherever robovast is installed (the controller pod, the
    service pod). Reuses the *normal* pipeline with the rosbag steps skipped — the
    conversion Job already did those — so there is no second implementation of the
    postprocessing sequence. Returns ``(ok, message)``.

    *state*, when given, also receives each step's line as the live ``stage`` marker — the
    same wiring the local lane uses, so the campaign view narrates this phase identically on
    both. ``None`` leaves it logging only.
    """
    from robovast.execution.control_server import stage_output_callback  # noqa: PLC0415
    from robovast.results_processing.postprocessing import ROSBAG_JOB_NAMES  # noqa: PLC0415
    from robovast.results_processing.postprocessing import run_postprocessing

    return run_postprocessing(
        results_dir=results_dir, campaign=campaign_id, force=force,
        skip=sorted(set(skip or ()) | set(ROSBAG_JOB_NAMES)),
        output_callback=stage_output_callback(state, logger.info))


#: This Job pod runs in a separate context from the controller, so its stdout is
#: otherwise only a transient ``kubectl logs``. Teeing the conversion output to this
#: campaign-relative path lets it ride the wholesale ``/out`` mirror below into the
#: object store, where :func:`sync_outputs` lands it at
#: ``<campaign_root>/_execution/postprocessing.log`` — the POSTPROCESSING section of
#: the unified campaign log (the host stage then appends to the same file).
_POSTPROC_LOG = "/out/_execution/postprocessing.log"

#: Where the conversion records what it produced from what. Under ``/out`` for the same
#: reason the log is: the wholesale mirror below carries it into the object store, and
#: ``sync_outputs`` lands it beside the campaign's other ``_execution`` artifacts, where
#: the host stage picks it up.
#:
#: Without this the Job passed no ``--provenance-file`` at all, so every ``rosbags_*``
#: step produced its tables and recorded nothing -- and the host stage runs with those
#: steps *skipped*, so it had nothing to record either. A cluster campaign's
#: ``postprocessing_steps`` table therefore held one row (``resource_usage``, from the
#: host stage) while four steps had run. The local lane, which does pass one, was
#: unaffected -- so the provenance a campaign carries depended on the lane it ran on.
_ROSBAG_PROVENANCE = "/out/_execution/rosbags_provenance.json"


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
            "--provenance-file", _ROSBAG_PROVENANCE,
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


def _short_job_name(prefix: str, campaign: str, discriminator: str = "") -> str:
    """Build a Kubernetes Job name ``<prefix><campaign>[-<discriminator>]`` capped at 63.

    Kubernetes copies the Job's ``metadata.name`` verbatim into the pod template's
    ``job-name`` label, and label values may be at most 63 chars — so the *name*
    itself (not just the label-safe campaign) has to fit, otherwise the Job is
    rejected with ``spec.template.labels: ... must be no more than 63 characters``.
    Keep the readable head of the campaign and append a short hash so distinct
    campaigns that share a truncated head still map to distinct Job names.
    """
    # The discriminator says WHICH conversion of this campaign the Job is. A search
    # converts once per repetitions-group, and while the name was the campaign's alone the
    # second create returned 409, fell through to the FIRST conversion's completed Job, and
    # reported success having converted nothing.
    identity = f"{campaign}-{discriminator}" if discriminator else campaign
    safe = re.sub(r"[^a-z0-9.-]", "", identity.lower().replace("_", "-").replace("/", "-"))
    full = f"{prefix}{safe}"
    if len(full) <= 63:
        return full
    # Hashed over the DISCRIMINATED identity, so truncation is not what makes two
    # conversions collide again.
    digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
    head = safe[: 63 - len(prefix) - 1 - len(digest)].rstrip("-.")
    return f"{prefix}{head}-{digest}"


#: Prefix for the per-campaign ConfigMap that carries the conversion scripts.
_SCRIPTS_CM_PREFIX = "robovast-postproc-scripts-"


def _scripts_cm_name(campaign_id: str, discriminator: str = "") -> str:
    """Discriminated with its Job: each conversion deletes this when it finishes, so a
    shared name lets a finishing conversion delete one another is still mounting."""
    return _short_job_name(_SCRIPTS_CM_PREFIX, campaign_id, discriminator)


def scripts_configmap_manifest(campaign_id: str, namespace: str,
                               discriminator: str = "") -> dict:
    """A ConfigMap carrying the *driver's own* conversion scripts.

    Built from ``robovast.results_processing.data`` — the same package dir the local
    path bind-mounts via ``docker_exec.sh -v <scripts>:/scripts``. Mounting this in the
    conversion Job (instead of copying ``/scripts`` from a separately-versioned
    controller image) makes the in-cluster scripts always match the driver that
    generated the conversion command, so the driver/script version skew that produced
    the ``--output-root`` failure cannot occur on any exec variant. The scripts are
    self-contained (stdlib + ROS2 libs + one sibling, no ``robovast`` import) and small
    (well under the 1 MiB ConfigMap limit), so a plain text ConfigMap suffices.
    """
    from importlib.resources import files  # noqa: PLC0415

    from .cluster_execution import _label_safe_campaign  # noqa: PLC0415

    data_dir = files("robovast.results_processing.data")
    payload = {}
    for entry in data_dir.iterdir():
        if entry.name == "__pycache__" or not entry.is_file():
            continue
        payload[entry.name] = entry.read_text(encoding="utf-8")
    if "rosbags_process.py" not in payload:
        raise RuntimeError(
            "conversion scripts not found in robovast.results_processing.data; "
            "cannot build the postprocessing ConfigMap")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _scripts_cm_name(campaign_id, discriminator),
            "namespace": namespace,
            "labels": {"jobgroup": "postprocessing",
                       "campaign-id": _label_safe_campaign(campaign_id)},
        },
        "data": payload,
    }


def job_failed_message(job_name: str) -> str:
    """What a failed conversion Job reports to the user.

    Named so the string has one definition and a test can hold it to its contract: it
    carries **no cluster command**. It lands on ``postprocessing_error``, which the web UI
    renders to someone who has a log panel and no kubeconfig; it used to append
    ``kubectl logs job/<name> -n <ns>``, which was unrunnable for that reader, aimed at
    whichever cluster their context happened to name, and pointed at a Job that
    ``ttlSecondsAfterFinished`` reaps 300 s after it fails -- so by the time most people
    read it, it named nothing that still existed. The conversion output is in the campaign
    log, which every surface already shows.
    """
    return (f"postprocessing job {job_name} failed — see the POSTPROCESSING section of "
            f"the campaign log for the conversion error")


def _blocked_reason(core, namespace: str, job_name: str) -> str:
    """``"<reason>: <message>"`` when this Job's pod cannot start, else ``""``.

    Reuses the signal the run loop and the image build already act on rather than reading pod
    status again here, so all three agree about what "blocked" means. Advisory: a pod list that
    cannot be read must not turn a running conversion into a reported failure, so an error here
    yields ``""`` and the wait continues to its own deadline.
    """
    from .cluster_execution import blocked_job_reasons  # noqa: PLC0415

    try:
        return blocked_job_reasons(core, namespace, f"job-name={job_name}").get(job_name, "")
    except Exception as e:  # noqa: BLE001 - advisory only
        logger.debug("Could not check whether %s is blocked: %s", job_name, e)
        return ""


def build_manifest(campaign_id: str, image: str, rosbag_cmds: list, s3: tuple,
                   namespace: str, force: bool = False,
                   pull_secret_name: str = "", discriminator: str = "") -> dict:
    """Build the conversion Job manifest.

    Args:
        campaign_id: The campaign to convert.
        image: **The campaign's execution image** (the SUT image from
            ``_execution/execution.yaml``) — required for its custom ROS2 types.
        rosbag_cmds: :func:`rosbag_commands_for` output.
        s3: ``(endpoint, access_key, secret_key, bucket, campaign_prefix)``.
        namespace: Kubernetes namespace.
        force: Bypass the per-rosbag caches.
        pull_secret_name: Secret for this pod's OWN image pulls -- the sidecar that mirrors
            the bags, and the campaign's execution image. Missing entirely until a
            private-registry deployment sat in ``ImagePullBackOff`` while the Job stayed
            ``active``, so the wait below reported a timeout and named neither the image nor
            the registry. Same omission the build Job had, in the same direction.

    The ``/scripts`` come from a per-campaign ConfigMap (see
    :func:`scripts_configmap_manifest`) built from the driver's own
    ``results_processing/data`` — the K8s analog of ``docker_exec.sh``'s
    ``-v <scripts>:/scripts`` — so the script version always matches the driver.
    """
    from .cluster_execution import _label_safe_campaign  # noqa: PLC0415

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
            "name": _short_job_name("robovast-postproc-", campaign_id, discriminator),
            "namespace": namespace,
            "labels": {
                "jobgroup": "postprocessing",
                "campaign-id": safe,
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
                    **({"imagePullSecrets": [{"name": pull_secret_name}]}
                       if pull_secret_name else {}),
                    "volumes": [
                        # The driver's own conversion scripts, executable (0755).
                        {"name": "scripts",
                         "configMap": {"name": _scripts_cm_name(campaign_id,
                                                                    discriminator),
                                       "defaultMode": 0o755}},
                        {"name": "tools", "emptyDir": {}},
                        {"name": "bags", "emptyDir": {}},
                        {"name": "out", "emptyDir": {}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                    "initContainers": [
                        {
                            # mc for the upload + mirror the campaign (rosbags) down.
                            # (The scripts are no longer copied by an initContainer —
                            # they arrive read-only from the ConfigMap volume above.)
                            "name": "s3-init",
                            "image": resolve_sidecar_image(),
                            "command": ["sh", "-c",
                                        'cp "$(command -v mc)" /tools/mc && '
                                        'chmod +x /tools/mc; '
                                        'mc alias set mystore "$S3_ENDPOINT" "$S3_ACCESS_KEY" '
                                        '"$S3_SECRET_KEY" && '
                                        'mc mirror "mystore/$S3_BUCKET/$S3_CAMPAIGN_PREFIX" /bags/'],
                            "env": s3_env,
                            "volumeMounts": [
                                {"name": "tools", "mountPath": "/tools"},
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
                       rosbag_cmds: list, force: bool = False,
                       timeout: int = _DEFAULT_TIMEOUT, kube_context=None,
                       discriminator: str = "") -> tuple:
    """Create the conversion Job and wait for it. Returns ``(ok, message)``.

    A no-op success when the campaign configures no rosbag conversion.

    *discriminator* names WHICH conversion of this campaign this is, and must be set by
    any caller that converts the same campaign more than once -- a search, which converts
    once per repetitions-group. Without it the second create returns 409 and the wait below
    reads the FIRST conversion's already-completed Job, returning "rosbag conversion
    complete" having converted nothing. Left empty the Job keeps the one-shot
    campaign-level name, where the 409 fallthrough is right: a retry of a single conversion
    should wait on the Job already in flight rather than launch a second copy.
    """
    if not rosbag_cmds:
        return True, "no rosbag conversion configured; nothing to run"

    from kubernetes import client  # noqa: PLC0415
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    from . import in_pod_storage  # noqa: PLC0415

    bucket, campaign_prefix = in_pod_storage.campaign_storage_location(
        cluster_config, campaign_id)
    access_key, secret_key = cluster_config.get_s3_credentials()
    s3 = (cluster_config.get_s3_endpoint(), access_key, secret_key, bucket, campaign_prefix)

    from robovast.common.errors import ClusterUnreachableError  # noqa: PLC0415

    from .cluster_execution import resolve_pull_secret  # noqa: PLC0415
    from .kube_client import load_kube_config  # noqa: PLC0415

    # Explicitly, and it must stay explicit: these clients read whatever context is loaded
    # when they are constructed. This used to be a side effect of the Kueue admission check
    # that ran above, and when that check was called without a context postprocessing dialled
    # the ambient kubeconfig while the campaign's Jobs had gone to the service's --context
    # cluster -- failing against a cluster the campaign never used, and naming the configured
    # API server as unreachable while quoting a timeout to a different address. Retiring Kueue
    # removed the check; the load it was incidentally doing is a real requirement and stays.
    load_kube_config(kube_context)
    core = client.CoreV1Api()
    batch = client.BatchV1Api()
    manifest = build_manifest(
        campaign_id, image, rosbag_cmds, s3, namespace, force=force,
        pull_secret_name=resolve_pull_secret(cluster_config, core, namespace),
        discriminator=discriminator)
    name = manifest["metadata"]["name"]

    # The conversion scripts arrive as a per-campaign ConfigMap mounted at /scripts —
    # the driver's own copy, so no controller-image version skew. Create it before the
    # Job (the pod waits in ContainerCreating until the volume source exists) and delete
    # it once the Job is done.
    cm = scripts_configmap_manifest(campaign_id, namespace, discriminator=discriminator)
    cm_name = cm["metadata"]["name"]
    # First call that actually touches the API server, so it is where an unreachable
    # cluster surfaces. Reported as a reason on the campaign's postprocessing_error and
    # re-runnable once the cluster is back -- the runs themselves are already published --
    # rather than reaching the caller as a urllib3 traceback.
    try:
        with api_transport_errors("submitting the postprocessing job"):
            try:
                core.create_namespaced_config_map(namespace=namespace, body=cm)
            except ApiException as e:
                if e.status == 409:  # a stale copy from a prior run — replace it
                    core.replace_namespaced_config_map(name=cm_name, namespace=namespace,
                                                       body=cm)
                else:
                    return False, f"could not create postprocessing scripts ConfigMap: {e}"
    except ClusterUnreachableError as e:
        return False, f"postprocessing cannot be scheduled: {e}"

    try:
        try:
            batch.create_namespaced_job(namespace=namespace, body=manifest)
        except ApiException as e:
            if e.status != 409:  # already exists → fall through and wait on it
                return False, f"could not create postprocessing job: {e}"
        logger.info("Postprocessing job %s created (image=%s)", name, image)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = batch.read_namespaced_job_status(
                    name=name, namespace=namespace).status
            except ApiException as e:
                if e.status == 404:  # reaped (ttl) — treat as finished
                    return True, "postprocessing job finished (reaped)"
                return False, f"could not read postprocessing job status: {e}"
            if not status.active:
                if status.succeeded:
                    logger.info("Postprocessing job %s succeeded", name)
                    return True, "rosbag conversion complete"
                if status.failed:
                    return False, job_failed_message(name)
            # A pod that CANNOT start leaves the Job `active` forever, so the polling above
            # never sees a verdict and this returns "timed out" -- naming a duration where the
            # cause was an unpullable image or an unschedulable pod. Same reasoning as the
            # Kueue admission check before submission, and the same signal the run loop and the
            # image build already act on.
            blocked = _blocked_reason(core, namespace, name)
            if blocked:
                return False, (
                    f"postprocessing job {name} cannot start: {blocked}. The conversion runs "
                    f"in the campaign's own execution image and mirrors its bags with the "
                    f"sidecar, so this is about pulling or scheduling those -- not about the "
                    f"conversion, which has not run. Nothing about the campaign's results is "
                    f"wrong; re-run postprocessing once the pod can start.")
            time.sleep(_POLL_SECONDS)
        return False, f"postprocessing job {name} timed out after {timeout}s"
    finally:
        # Best-effort cleanup of the scripts ConfigMap (labeled for manual sweep if the
        # driver dies before this runs). The Job's own ttlSecondsAfterFinished reaps it.
        try:
            core.delete_namespaced_config_map(name=cm_name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Could not delete scripts ConfigMap %s: %s", cm_name, e)
