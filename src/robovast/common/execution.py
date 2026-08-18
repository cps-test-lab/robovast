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

import copy
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from importlib.resources import files
from pprint import pformat

import yaml

from .common import convert_dataclasses_to_dict, get_scenario_parameters
from .config import SIMULATION_CONTAINER
from .config_identifier import compute_config_identifier, hash_file_content, hash_run_files
from .errors import CampaignConfigError, missing_input_error
from .simulators import SIM_CONFIG_FILE

# Compatibility version between host robovast code and the container image.
# Bump this integer when the contract between host scripts and the container
# changes (e.g. new required package, ROS distro change, script interface
# change).  The same value must appear in the Dockerfile as
# /etc/robovast_compat_version.
COMPAT_VERSION = 2

# The unprivileged user a robovast execution image runs as (fixuid is configured for it). Experiment
# image builds step up to root for apt/pip and must drop back to this, or a cluster pod -- which
# takes the user from the image, unlike a local run, where compose sets it explicitly -- would run
# the scenario as root.
DEFAULT_IMAGE_USER = "ubuntu:ubuntu"


# -- the RoboVAST image family ---------------------------------------------------------
#
# A family image ref glues three independent facts into one string, and authoring all
# three in one place is what made image configuration a five-variable problem:
#
#   harbor.example.org/robovast / robovast-roqsim : latest
#   \_______ WHERE ____________/   \____ WHAT ___/   \ WHICH /
#      deployment config             never a choice    version
#
# WHAT follows from the container's role and the campaign's mode, so it is never
# authored: a ``.vast`` names its OWN images and nothing else. That leaves one knob for
# WHERE (:envvar:`ROBOVAST_PROJECT`) and one for WHICH
# (:envvar:`ROBOVAST_PROJECT_TAG`), and the member is referred to symbolically until
# core resolves it -- exactly the treatment :data:`BUILD_IMAGE_PREFIX` gets below, and
# for the same reason: the author does not hold the context the ref needs.

#: Marks a ref that names a *member of the published set* rather than a concrete image:
#: ``family:<member>``. Resolved to ``<project>/<member>:<tag>`` by
#: :func:`resolve_family_image`; like a ``build:`` ref, one reaching a pod or compose
#: spec unresolved is a hard error rather than an image name nothing can pull.
FAMILY_IMAGE_PREFIX = "family:"

#: The RoboVAST contract image: ROS, scenario-execution, the VNC stack, ``/out``, the
#: compat marker. What a campaign runs in when no simulator adds to it, and the ``FROM``
#: every experiment image builds on. Carries no ``robovast`` Python package.
MEMBER_ROBOVAST = "robovast"
#: ``MEMBER_ROBOVAST`` plus roqsim and MuJoCo. Used by **both** roqsim shapes: stepped
#: in-process (where the scenario container *is* the simulator) and the ROS shape (where
#: the simulator gets its own container) -- it is the only image carrying both roqsim and
#: the RoboVAST contract, so one member serves both roles.
MEMBER_ROQSIM = "robovast-roqsim"
#: The service/API/UI host (``vast serve``). Python only -- no ROS, no GL.
MEMBER_CONTROLLER = "robovast-controller"
#: The small alpine helper (mc + boto3) used by object-store init containers and the
#: postprocessing Job.
MEMBER_SIDECAR = "robovast-sidecar"

#: Every member of the published set. A member is a *repository* name under the project,
#: so the ROS distro is a tag and never part of the name -- ``container/robovast/build.sh``,
#: ``container/release_images.sh`` and ``.github/workflows/image.yml`` publish exactly
#: these four, and ``tests/common/test_image_defaults.py`` keeps them in agreement.
#: Enumerated so a typo fails here rather than as a 404 at pull time, on a node.
FAMILY_MEMBERS = (MEMBER_ROBOVAST, MEMBER_ROQSIM, MEMBER_CONTROLLER, MEMBER_SIDECAR)

#: Where the published set lives when :envvar:`ROBOVAST_PROJECT` says nothing.
DEFAULT_IMAGE_PROJECT = "ghcr.io/cps-test-lab"

#: The tag used when neither :envvar:`ROBOVAST_PROJECT_TAG` nor a release version applies.
#: A floating tag, so resolving to it warns -- see :func:`default_image_tag`.
FLOATING_IMAGE_TAG = "latest"


# Marks an image ref that is *produced by a build* rather than pulled: ``build:<name>``,
# where the name is the container whose packages produced it. Internal only -- no .vast
# writes one, and it is resolved to a concrete image before reaching a pod/compose spec
# by the build lifecycle (which knows the registry + digest). It survives as the guard
# that catches an unresolved ref reaching a container spec, where it would otherwise be
# used verbatim as an invalid image name.
BUILD_IMAGE_PREFIX = "build:"


def is_build_image_ref(image: str | None) -> bool:
    """True if *image* is a symbolic ``build:<tag>`` ref (an unresolved build)."""
    return bool(image) and image.strip().startswith(BUILD_IMAGE_PREFIX)


def build_image_tag(image: str) -> str:
    """The bare ``<tag>`` from a ``build:<tag>`` symbolic ref."""
    return image.strip()[len(BUILD_IMAGE_PREFIX):]


def family_image_ref(member: str) -> str:
    """The symbolic ``family:<member>`` ref for *member*."""
    if member not in FAMILY_MEMBERS:
        raise ValueError(
            f"unknown RoboVAST image family member {member!r}; "
            f"expected one of: {', '.join(FAMILY_MEMBERS)}")
    return FAMILY_IMAGE_PREFIX + member


def is_family_image_ref(image: str | None) -> bool:
    """True if *image* is a symbolic ``family:<member>`` ref."""
    return bool(image) and image.strip().startswith(FAMILY_IMAGE_PREFIX)


def family_member(image: str) -> str:
    """The ``<member>`` from a ``family:<member>`` ref, validated against the set."""
    member = image.strip()[len(FAMILY_IMAGE_PREFIX):]
    if member not in FAMILY_MEMBERS:
        raise CampaignConfigError(
            f"unknown RoboVAST image family member {member!r} in {image!r}; "
            f"expected one of: {', '.join(FAMILY_MEMBERS)}")
    return member


def default_image_project() -> str:
    """The project (registry/namespace) the image family is pulled from.

    ``ROBOVAST_PROJECT`` is the *one* image knob: it moves the whole set at once, which
    is why there are no per-image variables. Five of those existed and three were never
    propagated into the in-cluster service, so an operator could set them all and still
    run the published images -- a knob per image is a knob per place to forget.
    """
    return os.environ.get("ROBOVAST_PROJECT", "").strip() or DEFAULT_IMAGE_PROJECT


def default_image_tag() -> str:
    """The tag the image family is pulled at.

    :data:`FLOATING_IMAGE_TAG` unless ``ROBOVAST_PROJECT_TAG`` pins one, and resolving to
    it warns (see :func:`resolve_family_image`).

    Deriving it from the installed version instead -- so a client and its images would be
    in step with nobody pinning either -- was tried and is wrong: it assumes every version
    has a published tag. This project is at 2.0.0 with no ``v2`` tag ever pushed, and CI
    publishes semver tags only for ``v*`` pushes, so the derived default named an image
    that does not exist. A default has to be a tag CI produces on every merge to the
    default branch, and ``latest`` is the only one that is.
    """
    return os.environ.get("ROBOVAST_PROJECT_TAG", "").strip() or FLOATING_IMAGE_TAG


def resolve_family_image(image: str, *, project: str | None = None,
                         tag: str | None = None, role: str = "container image") -> str:
    """Resolve a ``family:<member>`` ref to ``<project>/<member>:<tag>``.

    *project* and *tag* come from the campaign when there is one (so a single campaign
    can run against a dev project without touching the deployment) and from the
    environment otherwise. They are passed explicitly rather than read from ambient
    state here because a campaign is composed in a worker thread, and sometimes in an
    isolated subprocess -- neither of which a module-level value survives, and both of
    which run concurrently with campaigns configured differently.
    """
    member = family_member(image)
    resolved = f"{project or default_image_project()}/{member}:{tag or default_image_tag()}"
    if resolved.endswith(f":{FLOATING_IMAGE_TAG}"):
        # The spirit of the pinning rule this replaced: a run whose image is a floating
        # tag is not reproducible, and the person who has to know that is the one
        # starting it. Not an error -- a floating tag is the right answer for a dev loop
        # and for an editable install, which has no release tag to match.
        logger.warning(
            "%s resolved to %r, a floating tag: what this runs against is whatever was "
            "last pushed there. Set ROBOVAST_PROJECT_TAG to pin it.", role, resolved)
    return resolved


def resolve_family_images_in_containers(containers: dict | None, *,
                                        project: str | None = None,
                                        tag: str | None = None) -> dict | None:
    """Resolve every ``family:`` ref in an ``execution.containers`` mapping, in place.

    Called once per campaign, right after a simulator backend has filled its container
    blocks in, so that everything downstream -- the container plan, the image builds, the
    run environment, and ``_execution/execution.yaml`` -- reads concrete refs. Resolving
    later instead would leave a ``family:`` ref in the campaign's own record, and
    postprocessing reads that record to pick the image it deserializes rosbags in.

    Only ``family:`` refs are touched. A ref the ``.vast`` states is left byte-identical,
    digest and all: that field names the campaign's own image, and rewriting it would run
    something the author did not ask for.
    """
    for name, block in (containers or {}).items():
        if not isinstance(block, dict) or not is_family_image_ref(block.get("image")):
            continue
        block["image"] = resolve_family_image(
            block["image"], project=project, tag=tag,
            role=f"image for container '{name}'")
    return containers


def _resolve_image(member: str | None, *, explicit: str | None = None,
                   config_image: str | None = None, project: str | None = None,
                   tag: str | None = None, role: str = "container image") -> str:
    """Resolve a container image with a fixed precedence.

    Precedence (highest first): *explicit* (e.g. a ``--image`` flag) → *config_image*
    (a value from the ``.vast``) → the family default for *member*.

    Whatever wins, a symbolic ref is resolved here and only here. There are two, and the
    difference matters: a ``build:`` ref must already have been made concrete by the
    build lifecycle, so one arriving here is a bug and raises; a ``family:`` ref is
    *meant* to arrive symbolic, because this is the layer that knows the project and tag.

    *member* is ``None`` where there is no family default to fall back on — a sidecar
    container, whose image nothing but the campaign can name. Guessing the framework
    image for it would run something nobody asked for, so it fails loudly instead.
    """
    if explicit:
        resolved = explicit
    elif config_image:
        resolved = config_image
    elif member:
        resolved = family_image_ref(member)
    else:
        # CampaignConfigError, not ValueError: this is bad input with a self-contained,
        # actionable message, and `failure_detail` drops the traceback for it. A stack
        # trace here reads as a RoboVAST bug rather than as something the author has to
        # go and configure -- the same reason cluster_service raises it for an
        # unconfigured registry.
        raise CampaignConfigError(
            f"no image configured for this {role}: set "
            "execution.containers.<name>.image in the .vast. There is no family default "
            "for a container RoboVAST does not own — an image nobody named is not a "
            "default.")
    if is_build_image_ref(resolved):
        raise CampaignConfigError(
            f"unresolved build image ref '{resolved}': the 'build:' image must be "
            "built (build_experiment_image / the start_campaign preflight) before "
            "it can be used as a container image")
    if is_family_image_ref(resolved):
        resolved = resolve_family_image(resolved, project=project, tag=tag, role=role)
    return resolved


def resolve_robovast_image(explicit: str | None = None,
                           config_image: str | None = None, *,
                           fallback: bool = True, project: str | None = None,
                           tag: str | None = None) -> str:
    """Resolve the image a campaign's scenario container runs in.

    Defaults to :data:`MEMBER_ROBOVAST` — the RoboVAST contract image — which is also
    the ``FROM`` experiment images build on (:func:`resolve_build_base_image`).

    *fallback* is ``False`` for a container that is **not** the main one: a sidecar or
    system-under-test has no family default, and inventing one would launch an image the
    campaign never named.
    """
    return _resolve_image(MEMBER_ROBOVAST if fallback else None,
                          explicit=explicit, config_image=config_image,
                          project=project, tag=tag)


def resolve_build_base_image(config_image: str | None = None, *,
                             project: str | None = None,
                             tag: str | None = None) -> str:
    """Resolve the ``FROM`` an experiment image is built on (``build.base_image``).

    Spelled out separately from :func:`resolve_robovast_image` so the *role* in any
    warning names ``build.base_image``: a campaign reaching here has its
    ``execution.image`` set (to the ``build:`` ref that got us here), so a warning about
    ``execution.image`` reads as a bug in the campaign that is not there.
    """
    return _resolve_image(MEMBER_ROBOVAST, config_image=config_image,
                          project=project, tag=tag, role="build base image")


def resolve_controller_image(explicit: str | None = None,
                             config_image: str | None = None) -> str:
    """Resolve the robovast-controller image (the ``vast serve`` Deployment).

    Cluster-side and never per-campaign: this image is chosen when the service is
    deployed, so it takes the project from the environment ``vast exec cluster
    upgrade`` runs in.
    """
    return _resolve_image(MEMBER_CONTROLLER, explicit=explicit,
                          config_image=config_image, role="controller image")


def resolve_sidecar_image(explicit: str | None = None) -> str:
    """Resolve the robovast-sidecar image (object-store init + postprocessing Job).

    Resolved *inside* the service (the s3-init container, the mc-tools aux container,
    the postprocessing Job, campaign Jobs and the image-build Job all call this from
    there), so the project it uses is the one carried into the service pod's
    environment — see :func:`~...service_deploy.service_manifests`.
    """
    return _resolve_image(MEMBER_SIDECAR, explicit=explicit, role="sidecar image")


def get_app_version() -> str:
    """Return a short version string for the robovast package.

    Resolution order:
    1. Git short SHA (works for local editable installs).
       If the working tree has uncommitted changes, ``+dirty`` is appended.
    2. Installed package metadata (works for PyPI installs).
    3. ``"unknown"`` as a last-resort fallback.
    """
    module_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. Try Git
    try:
        sha = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            stderr=subprocess.STDOUT,
            cwd=module_dir,
            text=True,
        ).strip()
        # Detect uncommitted changes
        dirty = subprocess.check_output(
            ['git', 'status', '--porcelain'],
            stderr=subprocess.STDOUT,
            cwd=module_dir,
            text=True,
        ).strip()
        return f"{sha}+dirty" if dirty else sha
    except Exception:
        pass

    # 2. Fall back to installed package metadata
    try:
        return pkg_version('robovast')
    except PackageNotFoundError:
        pass

    # 3. Final fallback
    return 'unknown'


logger = logging.getLogger(__name__)


def _check_static_cpu_manager(k8s_client, node_name):
    """Query kubelet configz endpoint to determine the CPU manager policy for a node.

    Args:
        k8s_client: CoreV1Api instance
        node_name: Name of the node to query

    Returns:
        str or None: The cpuManagerPolicy value (e.g. "static", "none") when it
        could be read, or ``None`` when the query failed. ``None`` means *unknown*,
        never "policy is none" — the two are recorded and warned about differently,
        so a failed configz read is never silently logged as pinning-disabled.
    """
    try:
        response = k8s_client.connect_get_node_proxy_with_path(node_name, "configz")
        data = json.loads(response)
        kubelet_config = data.get("kubeletconfig", {})
        return kubelet_config.get("cpuManagerPolicy")
    except Exception as exc:
        logger.debug("Could not retrieve kubelet configz for node %s: %s", node_name, exc)
        return None


def _get_cluster_info(context=None):
    """Collect basic cluster information for cluster executions.

    Args:
        context: Kubernetes context name to use. ``None`` uses the active context.

    Returns a dictionary with node_count, node_labels, cpu_manager_policy and
    cluster_config (read from the deployed robovast-service) when available.
    Failures are logged and result in partial or empty data rather than errors.
    """
    cluster_info = {}

    # Best-effort cluster-config metadata, read from the deployed robovast-service
    # (the authoritative record; there is no local flag file). Non-fatal.
    try:
        from robovast.execution.cluster_execution.service_deploy import \
            read_service_config_from_cluster  # pylint: disable=import-outside-toplevel
        name, kwargs = read_service_config_from_cluster(kube_context=context)
        if name is not None:
            cluster_info["cluster_config"] = {"name": name, "kwargs": kwargs}
    except Exception as exc:  # pragma: no cover - best-effort, non-fatal
        # Expected wherever the service is not reachable (no kubeconfig, in-pod
        # controller, etc.); keep at debug to avoid noise.
        logger.debug("Cluster config metadata unavailable: %s", exc)

    # Collect node information via Kubernetes Python API
    node_count = None
    node_labels = {}
    cpu_manager_policies = {}
    try:
        from kubernetes import client as k8s_client_lib  # pylint: disable=import-outside-toplevel

        from robovast.execution.cluster_execution.kube_client import \
            load_kube_config  # pylint: disable=import-outside-toplevel
        load_kube_config(context=context)

        v1 = k8s_client_lib.CoreV1Api()
        node_list = v1.list_node()
        items = node_list.items or []
        node_count = len(items)

        for node in items:
            name = node.metadata.name
            labels = node.metadata.labels or {}
            if name:
                node_labels[name] = labels
                policy = _check_static_cpu_manager(v1, name)
                if policy is None:
                    # Query failed: unknown, not "none". Surface it — a node whose
                    # policy we cannot read may lack the pinning that deterministic
                    # scenario timing needs, and we must not record it as "none".
                    logger.warning(
                        "Could not determine CPU manager policy for node %s; "
                        "deterministic scenario timing is not guaranteed.", name)
                else:
                    cpu_manager_policies[name] = policy

        # Warn about nodes without static CPU pinning (recorded provenance keeps
        # only the policies we could actually read).
        non_static = [n for n, p in cpu_manager_policies.items() if p != "static"]
        if non_static:
            logger.warning(
                "Static CPU Manager policy is NOT enabled on %d node(s): %s; "
                "scenario timing may be non-deterministic.",
                len(non_static), ", ".join(non_static))

    except Exception as exc:  # pragma: no cover - best-effort, non-fatal
        logger.warning("Failed to collect cluster node information: %s", exc)

    if node_count is not None:
        cluster_info["node_count"] = node_count
    if node_labels:
        cluster_info["node_labels"] = node_labels
    if cpu_manager_policies:
        cluster_info["cpu_manager_policies"] = cpu_manager_policies

    return cluster_info or None


# Regex that matches any campaign directory name: <name>-YYYY-MM-DD-HHMMSS
# The default name prefix is "campaign" for backward compatibility.
_CAMPAIGN_DIR_RE = re.compile(r'^.+-\d{4}-\d{2}-\d{2}-\d{6,8}$')


def is_campaign_dir(name: str) -> bool:
    """Return True if *name* looks like a campaign directory.

    Both the legacy ``campaign-YYYY-MM-DD-HHMMSS`` format and the newer
    ``<metadata-name>-YYYY-MM-DD-HHMMSScc`` format (with hundredths of a
    second for concurrent-run disambiguation) are recognized.
    """
    return bool(_CAMPAIGN_DIR_RE.match(name))


def get_campaign_timestamp(dir_name: str) -> str:
    """Extract the timestamp portion from a campaign directory name.

    Works for both ``campaign-YYYY-MM-DD-HHMMSS`` and
    ``<name>-YYYY-MM-DD-HHMMSScc``.  Returns the full *dir_name* unchanged
    when the expected suffix cannot be found.
    """
    m = re.search(r'(\d{4}-\d{2}-\d{2}-\d{6,8})$', dir_name)
    return m.group(1) if m else dir_name


def get_campaign(name: str = "campaign") -> str:
    """Return a unique campaign directory name.

    Args:
        name: Campaign name prefix taken from ``metadata.name`` in the ``.vast``
              file.  Defaults to ``"campaign"`` for backward compatibility.

    Returns:
        A string of the form ``<name>-YYYY-MM-DD-HHMMSScc`` where *cc* are
        hundredths of a second.  The extra precision virtually eliminates
        campaign-ID collisions when multiple ``vast exec cluster run``
        invocations start in the same second.
    """
    now = datetime.datetime.now()
    return f"{name}-{now.strftime('%Y-%m-%d-%H%M%S')}"


def get_execution_env_variables(run_num, config_name, additional_env=None):
    """Get environment variables for execution.

    Args:
        run_num: Run number
        config_name: Configuration name
        additional_env: Optional list of additional environment variables in format:
                       [{"KEY": "value"}]

    Returns:
        Dictionary of environment variables
    """
    campaign_id = get_campaign()
    env_vars = {
        'CAMPAIGN_ID': campaign_id,
    }

    # Add custom environment variables from execution config
    if additional_env and isinstance(additional_env, list):
        for env_item in additional_env:
            if isinstance(env_item, dict):
                # Handle simple format: {"KEY": "value"}
                for key, value in env_item.items():
                    env_vars[key] = value

    return env_vars


def scenario_env(campaign_data):
    """The scenario-shaping env vars a run's config implies, for ``entrypoint.sh``.

    Covers only what is derived from the ``.vast`` and is therefore identical on every
    lane: which scenario file to run, the simulation backend, the runner selection, and
    whether the behaviour tree status log is recorded.
    Both execution backends built these separately (compose YAML lines vs a Kubernetes
    ``env`` list) from the same config keys, so the two could drift while looking
    correct; container-exec would have been a third copy.

    Deliberately *not* here:

    - **Path-valued vars** (``SCENARIO_PARAMETER_FILE``, ``OUTPUT_DIR``,
      ``SCENARIO_OUTPUT_DIR``). Those genuinely differ by lane, because the mount
      layout and job packing do — the caller owns them.
    - **``SCENARIO_EXECUTION_PARAMETERS``**. Its derivation is not yet common: the
      local lane builds ``-t``/``-d`` from ``log_tree``/``debug`` and otherwise defers
      to a ``run.sh`` shell variable, while the cluster lane knows only ``log_tree``.
      Sharing it would freeze that difference into one contract.
    """
    execution = campaign_data.get("execution") or {}
    env = {
        'SCENARIO_FILE': os.path.basename(
            campaign_data.get("scenario_file", "scenario.osc")),
    }
    simulation = execution.get("simulation")
    if simulation:
        env['SIMULATION'] = str(simulation)
    # 'auto' is the entrypoint's own default (detect ros2 on PATH); passing it would
    # only restate that, so it stays unset.
    mode = execution.get("mode", "auto")
    if mode and mode != "auto":
        env['SCENARIO_MODE'] = str(mode)
    # On by default: a run that did not record how its behaviour tree progressed cannot be
    # explained after the fact, and the file costs ~100 KB beside a multi-MB rosbag. Stated
    # either way rather than omitted when true, so the compose file / pod spec says outright
    # what the run did instead of leaving it to the entrypoint's own default.
    #
    # Not routed through SCENARIO_EXECUTION_PARAMETERS: the cluster lane overwrites that
    # whole variable with '-t', which would drop the flag on exactly the runs whose tree
    # state is hardest to inspect.
    #
    # An execution image whose scenario_execution predates --bt-log ignores the flag rather
    # than failing (both runners use parse_known_args), so the run still succeeds; it just
    # produces no behaviors.jsonl.
    env['BT_LOG'] = 'true' if execution.get("bt_log", True) else 'false'

    # What the entrypoint's own (wall-time) recorder captures. Stated for the same reason
    # BT_LOG is: the compose file / pod spec then says what the run recorded rather than
    # deferring to a default that may differ between image versions. An explicit empty
    # list records nothing, which the entrypoint reads as "skip the daemon".
    log_topics = execution.get("log_topics", ["/rosout", "/clock"])
    env['LOG_TOPICS'] = ' '.join(str(t) for t in (log_topics or []))

    # A simulator backend's environment, resolved *here* rather than by each emitter.
    # The three emitters disagreed about precedence -- compose let the later block win,
    # the cluster emitted duplicate keys and left it to the runtime, and container-exec
    # let the user win -- so a backend contribution would have meant something different
    # on each lane. One dict, one rule: the campaign's own execution.env wins, because a
    # backend supplies defaults it knows, not decisions it takes away.
    env.update(_backend_env_for(execution))
    return env


def _backend_env_for(execution: dict) -> dict:
    """The backend's contribution, with the campaign's own ``execution.env`` winning."""
    backend_env = execution.get("_backend_env") or {}
    if not backend_env:
        return {}
    authored = set()
    for entry in (execution.get("env") or []):
        authored.update(entry.keys() if isinstance(entry, dict) else [entry])
    return {k: str(v) for k, v in backend_env.items() if k not in authored}


def sidecar_backend_env(execution: dict, container_name: str) -> dict:
    """The backend's environment for a SIDECAR, which :func:`scenario_env` cannot reach.

    ``scenario_env`` emits the backend's contribution into the *main* container, which is
    right in the stepped shape: there the simulator runs in the scenario's own process, so
    the main container IS the simulator. In the ROS shape the simulator is a sidecar, and
    the same variables have to arrive there instead -- otherwise the simulator never sees
    them. That is not hypothetical: roqsim's ``ROQSIM_RECORD`` /
    ``ROQSIM_CAPTURE_EXPORT_DIR`` went only to the scenario container, so a ROS campaign
    produced no ``run.npz`` and no ``capture/`` while ``produces_run_capture()`` still
    reported True and validation happily accepted a ``scene3d`` panel with nothing to
    replay. The stepped shape hid it, because there the two containers are one.

    Only the ``simulation`` container: a backend describes its own simulator, and handing
    ``ROQSIM_*`` to a vanilla nav2 SUT would be noise that reads like configuration.

    A relative path in those variables resolves against ``RUN_OUTPUT_DIR``, which both
    lanes already give every sidecar -- so a per-run artifact lands in the run's own
    directory rather than at the campaign root where each run would overwrite the last.
    """
    if container_name != SIMULATION_CONTAINER:
        return {}
    return _backend_env_for(execution)


_LOCAL_INIT_BLOCK = "command -v fixuid > /dev/null 2>&1 || { echo 'ERROR: fixuid not found in container image. Please rebuild the image.' >&2; exit 1; }; eval $(fixuid -q)\nEXTRA_REQUIRED_TOOLS=\"fixuid\""

# Cluster runs mirror output to S3 in a post-run step (and pull config via an
# mc-based init container), so 'mc' must be present. Declared here so the
# entrypoint's tool check fails fast instead of crashing after a full run.
_CLUSTER_INIT_BLOCK = "EXTRA_REQUIRED_TOOLS=\"mc\""

# Used when the caller has no cluster provider to ask: a local Docker run is not an
# instance of anything, so the recorded instance_type is empty (which ingests as NULL).
# ``|| true`` is not needed here, but a provider's command must never abort the run —
# sysinfo collection is explicitly non-fatal — so each implementation keeps its own
# failure tolerance (a metadata-server curl that 404s yields an empty string).
_NO_INSTANCE_TYPE = 'INSTANCE_TYPE=""'

# The log helper both entrypoints share, substituted into ``# @@LOG_BLOCK@@``.
#
# One definition because the two scripts must emit the *same* line format: the merged run log
# parses one grammar (``robovast.common.log_summary._STAMP``), and a sidecar whose format drifted
# from the main container's would not error -- it would silently lose its timestamps and fall back
# to inheriting a neighbour's. Duplicated shell is how that drift happens.
#
# The format is deliberately the one every ROS node in these containers already writes, so the
# entrypoint's own lines need no second parser to be placed in time and attributed.
_LOG_BLOCK = """\
# The wall clock for a log stamp, formatted as ROS writes it: `1786264427.117714`.
#
# `EPOCHREALTIME` (bash 5) is preferred because it costs no subprocess, and this runs once per
# logged line. It is *locale-formatted*, though: under a comma-decimal locale it yields
# `1786264427,117714`, which the log grammar does not match and whose loss nothing would report
# -- the lines would simply have no time. Substituting the separator here is safer than forcing
# LC_NUMERIC on the whole container, whose workload we do not want to relocalise.
_now() {
    local t="${EPOCHREALTIME:-}"
    if [ -n "${t}" ]; then
        echo "${t/,/.}"
    else
        date -u +%s.%6N
    fi
}

# Log one line in the `[LEVEL] [epoch] [node]:` form described above.
#
# Writes to stdout ONLY. The redirect below tees stdout into ${LOG_FILE}, so teeing here as
# well -- which this did -- put every line of a non-ROS run's log in that file twice, and made
# every count derived from it wrong by up to 2x.
#
# The level comes from the message when the message declares one. Several call sites say
# "ERROR: ..." in their text, and a stamped level *wins* over the keyword scan that used to
# classify them, so hard-coding INFO here would quietly downgrade every one of those to
# routine output in the log panel's counts and in search_run_logs.
log() {
    local msg="$*" level=INFO
    case "${msg}" in
        ERROR:*|FATAL:*)  level=ERROR ;;
        WARNING:*|WARN:*) level=WARN ;;
    esac
    echo "[${level}] [$(_now)] [entrypoint]: ${msg}"
}"""

_LOCAL_POST_RUN_BLOCK = """\
    # Build built-in cleanup script (stop rosbag and resource monitor gracefully)
    BUILTIN_CLEANUP_SCRIPT="/tmp/robovast_cleanup.sh"
    cat > "${BUILTIN_CLEANUP_SCRIPT}" << 'CLEANUP_EOF'
#!/bin/bash
if [ -f /tmp/rosbag.pid ]; then
    if start-stop-daemon --stop --signal INT --pidfile /tmp/rosbag.pid >/dev/null 2>&1; then
        _t=0
        while kill -0 $(cat /tmp/rosbag.pid) 2>/dev/null && [ $_t -lt 50 ]; do
            sleep 0.1; _t=$((_t + 1))
        done
        kill -KILL $(cat /tmp/rosbag.pid) 2>/dev/null || true
    fi
    echo "ROS bag process stopped."
fi
if [ -f /tmp/monitor.pid ]; then
    if kill -TERM $(cat /tmp/monitor.pid) 2>/dev/null; then
        _t=0
        while kill -0 $(cat /tmp/monitor.pid) 2>/dev/null && [ $_t -lt 30 ]; do
            sleep 0.1; _t=$((_t + 1))
        done
        kill -KILL $(cat /tmp/monitor.pid) 2>/dev/null || true
    fi
    echo "Resource monitor process stopped."
fi
exit 0
CLEANUP_EOF
    chmod +x "${BUILTIN_CLEANUP_SCRIPT}"

    POST_COMMAND_PARAM="--post-run ${BUILTIN_CLEANUP_SCRIPT}"
    if [ -n "${POST_COMMAND}" ]; then
        if [ -e "${POST_COMMAND}" ]; then
            POST_COMMAND_PARAM="--post-run ${POST_COMMAND} --post-run ${BUILTIN_CLEANUP_SCRIPT}"
            log "Post-command '${POST_COMMAND}' will run before built-in cleanup."
        else
            log "ERROR: Post-command '${POST_COMMAND}' does not exist."
            exit 1
        fi
    fi"""

_CLUSTER_POST_RUN_BLOCK = """\
    # Build built-in cleanup script (stop rosbag and resource monitor gracefully)
    BUILTIN_CLEANUP_SCRIPT="/tmp/robovast_cleanup.sh"
    cat > "${BUILTIN_CLEANUP_SCRIPT}" << 'CLEANUP_EOF'
#!/bin/bash
echo "[cleanup] Starting robovast cleanup (PID=$$)..."
echo "[cleanup] Process tree at cleanup start:"
ps -eo pid,ppid,stat,args 2>/dev/null || ps ax 2>/dev/null || true
echo ""

_stop_daemon() {
    local _name="$1" _pidfile="$2" _signal="$3" _retry="$4"
    if [ ! -f "$_pidfile" ]; then
        echo "[cleanup] ${_name}: no pidfile at ${_pidfile}, skipping."
        return 0
    fi
    local _pid
    _pid=$(cat "$_pidfile" 2>/dev/null)
    if [ -z "$_pid" ]; then
        echo "[cleanup] ${_name}: pidfile ${_pidfile} is empty, removing."
        rm -f "$_pidfile"
        return 0
    fi
    local _state _ppid _comm
    _state=$(awk '/^State:/{print $2}' /proc/$_pid/status 2>/dev/null)
    _ppid=$(awk '/^PPid:/{print $2}' /proc/$_pid/status 2>/dev/null)
    _comm=$(cat /proc/$_pid/comm 2>/dev/null)
    if [ -z "$_state" ]; then
        echo "[cleanup] ${_name}: PID=$_pid not found in /proc (already exited), removing pidfile."
        rm -f "$_pidfile"
        return 0
    fi
    echo "[cleanup] ${_name}: PID=$_pid state=$_state ppid=$_ppid comm=$_comm"
    if [ "$_state" = "Z" ]; then
        echo "[cleanup] ${_name}: PID=$_pid is a zombie (ppid=$_ppid), cannot signal. Removing pidfile."
        rm -f "$_pidfile"
        return 0
    fi
    echo "[cleanup] ${_name}: sending ${_signal} to PID=$_pid (retry=${_retry})..."
    if start-stop-daemon --stop --signal "$_signal" --pidfile "$_pidfile" --retry "$_retry" --remove-pidfile --verbose 2>&1; then
        echo "[cleanup] ${_name}: stopped successfully."
    else
        local _rc=$?
        echo "[cleanup] ${_name}: start-stop-daemon exited with code $_rc."
        local _post_state
        _post_state=$(awk '/^State:/{print $2}' /proc/$_pid/status 2>/dev/null)
        echo "[cleanup] ${_name}: PID=$_pid post-stop state=${_post_state:-gone}"
        rm -f "$_pidfile"
    fi
}

_stop_daemon "rosbag" "/tmp/rosbag.pid" "INT" "INT/30/KILL/5"
_stop_daemon "monitor" "/tmp/monitor.pid" "TERM" "TERM/10/KILL/5"
echo "[cleanup] Cleanup finished."
CLEANUP_EOF
    chmod +x "${BUILTIN_CLEANUP_SCRIPT}"

    # Build the S3 upload script; output is mirrored to the S3 bucket after the run
    S3_UPLOAD_SCRIPT="/tmp/s3_upload.sh"
    cat > "${S3_UPLOAD_SCRIPT}" << 'UPLOAD_EOF'
#!/bin/bash
set -e
echo "[s3-upload] Starting S3 upload..."
echo "[s3-upload] Setting up mc alias for S3 endpoint..."
mc alias set mystore "${S3_ENDPOINT}" "${S3_ACCESS_KEY}" "${S3_SECRET_KEY}" --quiet
# Normalize the destination: S3_PREFIX may be empty (packed jobs on per-campaign
# buckets mirror to the bucket root) or carry a trailing slash; strip it so we
# never produce a "bucket//" double slash (which S3 treats as a leading-slash key).
S3_DEST="mystore/${S3_BUCKET}/${S3_PREFIX}"
S3_DEST="${S3_DEST%/}"
echo "[s3-upload] Mirroring /out/ to ${S3_DEST}/..."
# --overwrite, because every container in the job mirrors the SAME shared /out/ to the
# SAME prefix, in finishing order. Without it mc refuses an object whose size already
# differs ("Overwrite not allowed ... (size)") -- and it is always the LATER, more
# complete copy that gets refused: the main container uploads logs/system*.log and
# resource_usage_*.csv while they are still being appended, so every sidecar's upload of
# the finished file is rejected and the store keeps the earliest truncated snapshot. That
# is how an archived system.log came to end mid-sentence on its own "Mirroring /out/..."
# line. Later is strictly more complete here (a container only uploads after its workload
# and its resource monitor have stopped), so last-writer-wins is the correct resolution
# and not a race. Payload each container uniquely owns was never affected -- mc skips
# same-size objects, so this costs no extra transfer for the bag or the capture.
mc mirror --overwrite /out/ "${S3_DEST}/"
echo "[s3-upload] Mirror complete. Re-tagging executable files..."
# Re-tag executable files with x-amz-meta-executable metadata
_exec_count=0
find /out/ -type f -executable | while IFS= read -r f; do
    rel="${f#/out/}"
    mc cp --attr "x-amz-meta-executable=yes" "${S3_DEST}/${rel}" "${S3_DEST}/${rel}" --quiet
    _exec_count=$((_exec_count + 1))
done
echo "[s3-upload] S3 upload finished."
UPLOAD_EOF
    chmod +x "${S3_UPLOAD_SCRIPT}"

    POST_COMMAND_PARAM="--post-run ${BUILTIN_CLEANUP_SCRIPT} --post-run ${S3_UPLOAD_SCRIPT}"
    if [ -n "${POST_COMMAND}" ]; then
        if [ -e "${POST_COMMAND}" ]; then
            POST_COMMAND_PARAM="--post-run ${POST_COMMAND} --post-run ${BUILTIN_CLEANUP_SCRIPT} --post-run ${S3_UPLOAD_SCRIPT}"
            log "Post-command '${POST_COMMAND}' will run before built-in cleanup and S3 upload."
        else
            log "ERROR: Post-command '${POST_COMMAND}' does not exist."
            exit 1
        fi
    fi"""


def local_parameter_overrides(campaign_data, *, gui: bool) -> list:
    """The scenario-parameter overrides a **local** run applies, in precedence order.

    Two blocks, because they answer different questions:

    * ``execution.local.parameter_overrides`` — every local run, whatever it looks like.
    * ``execution.local.gui.parameter_overrides`` — only a run with the host display
      wired in, merged last so it wins.

    Keeping them apart is what lets a project say ``headless: "False"`` without it firing
    on a headless run, which would ask the scenario to open a window on a display that is
    not there. The condition is in the config *path* rather than in the meaning of an
    existing key, so ``execution.local.parameter_overrides`` still means exactly what it
    always did.

    Accepts either the raw mapping or a validated model at ``execution.local``, matching
    the two shapes callers already pass around.
    """
    local = (campaign_data.get("execution") or {}).get("local")
    if hasattr(local, "parameter_overrides"):
        base = local.parameter_overrides or []
        gui_block = getattr(local, "gui", None)
    elif isinstance(local, dict):
        base = local.get("parameter_overrides") or []
        gui_block = local.get("gui")
    else:
        return []
    overrides = list(base)
    if gui and gui_block is not None:
        if hasattr(gui_block, "parameter_overrides"):
            overrides += list(gui_block.parameter_overrides or [])
        elif isinstance(gui_block, dict):
            overrides += list(gui_block.get("parameter_overrides") or [])
    return overrides


def _apply_local_parameter_overrides(config, parameter_overrides, valid_param_names,
                                     scenario_name, scenario_path):
    """Apply local parameter overrides to config, validating against scenario parameters.

    Args:
        config: The scenario config dict to modify (will be mutated)
        parameter_overrides: List of dicts, each with a single key-value (e.g. [{"headless": False}])
        valid_param_names: Set or list of parameter names defined in the scenario
        scenario_name: Name of the scenario (for error messages)
        scenario_path: Path to scenario file (for error messages)

    Raises:
        ValueError: If any override key is not a valid scenario parameter
    """
    if not parameter_overrides:
        return
    merged = {}
    for item in parameter_overrides:
        if isinstance(item, dict):
            merged.update(item)
    if not merged:
        return
    valid_set = set(valid_param_names) if valid_param_names else set()
    invalid = [k for k in merged if k not in valid_set]
    if invalid:
        raise ValueError(
            f"Invalid parameter_overrides in execution.local for scenario '{scenario_name}': "
            f"{invalid}. Valid parameters in {scenario_path} are: {sorted(valid_set)}"
        )
    config.update(merged)


def check_campaign_inputs(campaign_data):
    """Fail with one actionable error if a required project input is missing.

    Staging copies the ``.vast``, the scenario file and the ``run_files`` verbatim,
    so a wrong path used to surface as ``shutil``'s ``[Errno 2] ... '<path>'`` —
    which names neither the ``.vast`` key the path came from nor what it was
    resolved against, and arrives mid-campaign, after the campaign dir and the
    store entry already exist. Checked up front and reported together instead, as
    the user error it is (no traceback; see :class:`CampaignConfigError`).
    """
    vast_file = campaign_data.get("vast")
    vast_dir = os.path.dirname(vast_file) if vast_file else ""
    candidates = [("the .vast file", vast_file, vast_file),
                  ("execution.scenario_file", campaign_data.get("scenario_file"),
                   campaign_data.get("scenario_file"))]
    # run_files are collected relative to the .vast's directory; _input_files and the
    # transient files are skipped-with-a-warning at their copy site (they are optional
    # extras, not something a run cannot start without), so they are not checked here.
    for run_file in campaign_data.get("_run_files", []):
        candidates.append(("execution.run_files", run_file,
                           os.path.join(vast_dir, run_file)))
    missing = [entry for entry in candidates
               if not entry[2] or not os.path.isfile(entry[2])]
    if missing:
        raise missing_input_error(missing)


def render_entrypoint(*, cluster=False, instance_type_command=None):
    """The container entrypoint script, with its lane-specific blocks substituted.

    The template carries three markers whose content depends on *where* the container
    runs: the init block (``fixuid`` locally, config fetch in-cluster), the post-run
    block (local cleanup vs mirroring results to S3), and the instance-type probe. A
    script rendered for one lane is therefore wrong on the other — which is why a
    campaign's staged ``entrypoint.sh`` must never be reused by something running
    elsewhere, and why container-exec renders its own instead of copying one.

    Separate from :func:`prepare_campaign_configs` because a diagnostic that runs a bare
    command needs the entrypoint *without* a config tree — and demanding a valid
    scenario file to produce one would fail checks that have nothing to do with the
    question being asked.
    """
    entrypoint_src = str(files('robovast.execution.data').joinpath('entrypoint.sh'))
    with open(entrypoint_src, 'r', encoding='utf-8') as f:
        content = f.read()
    init_block = _CLUSTER_INIT_BLOCK if cluster else _LOCAL_INIT_BLOCK
    post_run_block = _CLUSTER_POST_RUN_BLOCK if cluster else _LOCAL_POST_RUN_BLOCK
    content = content.replace('# @@INIT_BLOCK@@', init_block)
    content = content.replace('# @@LOG_BLOCK@@', _LOG_BLOCK)
    content = content.replace('# @@INSTANCE_TYPE_BLOCK@@',
                              instance_type_command or _NO_INSTANCE_TYPE)
    content = content.replace('    # @@POST_RUN_BLOCK@@', post_run_block)
    return content


def prepare_campaign_configs(out_dir, campaign_data, cluster=False,
                             instance_type_command=None, gui=False):
    """Stage a campaign's config tree, including the generated entrypoint.

    *gui* selects whether ``execution.local.gui.parameter_overrides`` is staged along with
    ``execution.local.parameter_overrides`` (see :func:`local_parameter_overrides`). It
    defaults to **off** so a caller that does not thread it through under-applies rather
    than staging a scenario that expects a window nobody asked for.

    *instance_type_command* is a shell line that sets ``INSTANCE_TYPE``, obtained from the
    cluster provider's
    :meth:`~robovast.execution.cluster_config.base_config.BaseConfig.get_instance_type_command`
    — the machine type on a cloud (GCP's metadata server, Azure's IMDS), the architecture
    on bare metal. The *caller* resolves it rather than this function looking a provider
    up, so ``robovast.common`` keeps no dependency on the cluster packages. Omitted, the
    recorded instance type is empty, which is the honest answer for a local Docker run.
    """
    # Create the output directory structure
    logger.debug(f"Campaign Configs: {pformat(campaign_data)}")
    check_campaign_inputs(campaign_data)
    os.makedirs(out_dir, exist_ok=True)

    campaign_config_dir = os.path.join(out_dir, "_config")
    os.makedirs(campaign_config_dir, exist_ok=True)

    campaign_transient_dir = os.path.join(out_dir, "_transient")
    os.makedirs(campaign_transient_dir, exist_ok=True)

    init_block = _CLUSTER_INIT_BLOCK if cluster else _LOCAL_INIT_BLOCK
    entrypoint_dst = os.path.join(campaign_transient_dir, "entrypoint.sh")
    with open(entrypoint_dst, 'w', encoding='utf-8') as f:
        f.write(render_entrypoint(cluster=cluster,
                                 instance_type_command=instance_type_command))

    # Copy secondary_entrypoint.sh into _transient/ (with init block replacement)
    secondary_entrypoint_src = str(files('robovast.execution.data').joinpath('secondary_entrypoint.sh'))
    with open(secondary_entrypoint_src, 'r', encoding='utf-8') as f:
        secondary_entrypoint_content = f.read()
    secondary_entrypoint_content = secondary_entrypoint_content.replace('# @@INIT_BLOCK@@', init_block)
    secondary_entrypoint_content = secondary_entrypoint_content.replace('# @@LOG_BLOCK@@', _LOG_BLOCK)
    secondary_entrypoint_dst = os.path.join(campaign_transient_dir, "secondary_entrypoint.sh")
    with open(secondary_entrypoint_dst, 'w', encoding='utf-8') as f:
        f.write(secondary_entrypoint_content)

    # Copy collect_sysinfo.py into _transient/
    collect_sysinfo_src = str(files('robovast.execution.data').joinpath('collect_sysinfo.py'))
    collect_sysinfo_dst = os.path.join(campaign_transient_dir, "collect_sysinfo.py")
    shutil.copy2(collect_sysinfo_src, collect_sysinfo_dst)

    # Copy the resource monitor into _transient/, from where it is mounted at /config in
    # every container.
    monitor_src = str(files('robovast.execution.data').joinpath('monitor_resources.py'))
    shutil.copy2(monitor_src, os.path.join(campaign_transient_dir, 'monitor_resources.py'))

    # Copy rosbag processing scripts into _transient/ for host-side post-run processing
    for script_name in ('rosbags_process.py', 'rosbags_common.py', 'ros2_exec.sh'):
        src = str(files('robovast.results_processing.data').joinpath(script_name))
        shutil.copy2(src, os.path.join(campaign_transient_dir, script_name))
    os.chmod(os.path.join(campaign_transient_dir, 'ros2_exec.sh'), 0o755)

    vast_file_path = os.path.dirname(campaign_data["vast"])

    # Prepare campaign_data for configurations.yaml (strip internal keys)
    campaign_data_for_dump = copy.deepcopy(campaign_data)
    campaign_data_for_dump.pop("_transient_files", None)
    campaign_data_for_dump.pop("_output_dir", None)
    for c in campaign_data_for_dump.get("configs", []):
        c.pop("_config_block", None)

    # Save scenario variations as YAML in _transient subdirectory
    scenario_variations_path = os.path.join(campaign_transient_dir, "configurations.yaml")
    with open(scenario_variations_path, 'w') as f:
        yaml.dump(convert_dataclasses_to_dict(campaign_data_for_dump), f, default_flow_style=False, sort_keys=False)
    logger.debug(f"Saved configurations to {scenario_variations_path}")

    # Compute hashes once per run (reused for all configs)
    run_files_hash = hash_run_files(vast_file_path, campaign_data.get("_run_files", []))
    # Config generation already resolved this against the .vast's location, so it is usable as-is
    # (see the same note in execute_local). Re-prepending the .vast's directory doubled it -- e.g.
    # `<project>/<project>/scenario.osc` -- for every project whose config path has a
    # directory part, and was a silent no-op only for the usual case of `vast init` run in the
    # project's own directory.
    scenario_file_path_for_hash = campaign_data["scenario_file"]
    scenario_file_hash = (
        hash_file_content(scenario_file_path_for_hash)
        if os.path.isfile(scenario_file_path_for_hash)
        else ""
    )

    # Copy scenario_file into _config/
    scenario_rel = os.path.basename(campaign_data["scenario_file"])
    scenario_config_dst = os.path.join(campaign_config_dir, scenario_rel)
    os.makedirs(os.path.dirname(scenario_config_dst), exist_ok=True)
    shutil.copy2(scenario_file_path_for_hash, scenario_config_dst)

    # Copy the .vast file into _config/
    vast_src = campaign_data["vast"]
    vast_dst = os.path.join(campaign_config_dir, os.path.basename(vast_src))
    shutil.copy2(vast_src, vast_dst)

    # Copy run files
    for config_file in campaign_data.get("_run_files", []):
        src_path = os.path.join(vast_file_path, config_file)
        dst_path = os.path.join(campaign_config_dir, config_file)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)

    # Copy variation input files and analysis notebooks into _config/
    for input_file in campaign_data.get("_input_files", []):
        src_path = os.path.join(vast_file_path, input_file)
        dst_path = os.path.join(campaign_config_dir, input_file)
        if not os.path.exists(src_path):
            logger.warning(f"Input file not found, skipping: {src_path}")
            continue
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)

    # Copy campaign-level transient files into _transient/
    for rel_path, abs_path in campaign_data.get("_transient_files", []):
        if not os.path.exists(abs_path):
            logger.warning(f"Transient file not found, skipping: {abs_path}")
            continue
        dst_path = os.path.join(campaign_transient_dir, rel_path)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(abs_path, dst_path)

    # get scenario name
    original_scenario_path = campaign_data.get("scenario_file")
    try:
        scenario_params = get_scenario_parameters(original_scenario_path)
        scenario_name = next(iter(scenario_params.keys()))

        if scenario_name is None:
            raise ValueError(f"Scenario name not found in {original_scenario_path}")
    except Exception as e:
        raise RuntimeError(f"Could not get scenario name from {original_scenario_path}: {e}") from e

    # Resolve valid scenario parameter names for parameter_overrides validation
    existing_scenario_parameters = next(iter(scenario_params.values())) if scenario_params else []
    valid_param_names = [
        p.get('name') for p in existing_scenario_parameters
        if isinstance(p, dict) and 'name' in p
    ]

    # Local-only scenario-parameter overrides; the gui half only when this run has a
    # display (see local_parameter_overrides).
    parameter_overrides = [] if cluster else local_parameter_overrides(
        campaign_data, gui=gui)

    for config_data in campaign_data["configs"]:
        run_config_dir = os.path.join(out_dir, config_data.get("name"), "_config")

        # Compute and write config identifier for merge-campaigns
        config_block = config_data.get("_config_block", {})
        variation_type_names = [
            v["name"] for v in config_data.get("_variations", [])
        ]
        config_identifier, sub_identifier = compute_config_identifier(
            vast_file_path,
            config_block,
            run_files_hash,
            scenario_file_hash,
            variation_type_names,
        )
        config_yaml_path = os.path.join(run_config_dir, "config.yaml")
        os.makedirs(run_config_dir, exist_ok=True)
        with open(config_yaml_path, "w") as f:
            yaml.dump(
                {"config_identifier": config_identifier, "sub_identifier": sub_identifier},
                f,
                default_flow_style=False,
                sort_keys=False,
            )

        # Copy config files
        # artifact paths may be relative to campaign_data["_output_dir"]; source
        # paths are always absolute.
        _gen_output_dir = campaign_data.get("_output_dir", "")
        if "_config_files" in config_data:
            for config_rel_path, config_path in config_data["_config_files"]:
                src_path = (
                    config_path
                    if os.path.isabs(config_path)
                    else os.path.join(_gen_output_dir, config_path)
                )
                if not os.path.exists(src_path):
                    # A generated artifact, not something the user wrote: the message
                    # has to say so, or the path reads like a bad .vast entry. The
                    # usual cause is a config-generation cache entry whose artifact
                    # tarball no longer matches, so name the cache as the remedy.
                    raise CampaignConfigError(
                        f"Config '{config_data.get('name')}': the generated config "
                        f"file '{config_rel_path}' is missing at {src_path}.\n"
                        "It is produced by config generation, not by the .vast — a "
                        "stale generation cache is the usual cause. Remove "
                        f"{os.path.join(vast_file_path, '.cache')} and retry.")
                dst_path = os.path.join(run_config_dir, config_rel_path)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy2(src_path, dst_path)

        # Copy config-level transient files into <config>/_transient/
        config_name = config_data.get("name", "")
        for rel_path, path in config_data.get("_config_transient_files", []):
            abs_path = (
                path
                if os.path.isabs(path)
                else os.path.join(_gen_output_dir, path)
            )
            if not os.path.exists(abs_path):
                logger.warning(f"Config transient file not found, skipping: {abs_path}")
                continue
            dst_path = os.path.join(out_dir, config_name, "_transient", rel_path)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(abs_path, dst_path)

        # The simulation channel's record, beside the scenario channel's. A record, not an
        # input: what the run reads is the per-job overrides file plus the world on argv.
        # It is here so that what the simulator was given is a file next to the
        # configuration it belongs to -- greppable, diffable, and directly replayable, its
        # two values being the arguments that reproduce the cell by hand.
        #
        # Written for EVERY configuration, including one that varies nothing, so a reader
        # never has to work out whether a missing file means "no simulator" or "nothing
        # varied".
        sim_block = config_data.get("sim")
        if sim_block:
            os.makedirs(run_config_dir, exist_ok=True)
            with open(os.path.join(run_config_dir, SIM_CONFIG_FILE), "w") as f:
                yaml.dump(convert_dataclasses_to_dict(copy.deepcopy(sim_block)), f,
                          default_flow_style=False, sort_keys=False)

        # Create config file if needed
        if "config" in config_data:
            config = config_data.get('config')
            if config is not None:
                config_dict = convert_dataclasses_to_dict(copy.deepcopy(config))
                if parameter_overrides:
                    _apply_local_parameter_overrides(
                        config_dict, parameter_overrides, valid_param_names,
                        scenario_name, original_scenario_path
                    )
                wrapped_config_data = {scenario_name: config_dict}
                dst_path = os.path.join(run_config_dir, 'scenario.config')
                os.makedirs(run_config_dir, exist_ok=True)
                with open(dst_path, 'w') as f:
                    yaml.dump(wrapped_config_data, f, default_flow_style=False, sort_keys=False)


def _namespace_file_params(value, deploy_paths, namespace_prefix):
    """Recursively rewrite file-valued scenario parameters to a namespaced path.

    When several configurations are packed into one job, each config's generated
    files are mounted under a per-config directory (``<namespace_prefix>/...``)
    to avoid name collisions. Any string parameter whose value equals one of the
    config's ``_config_files`` deploy paths is rewritten to
    ``<namespace_prefix>/<deploy_path>``. All other values are left untouched.

    The prefix is *relative to the config mount root* (i.e. the scenario file's
    own directory), because scenarios resolve file params against their own
    location (``get_scenario_file_directory() + "/" + value``). Making the value
    absolute here would double the mount root (``/config/config/...``).

    Args:
        value: A scenario-parameter value (scalar, list or dict) to walk.
        deploy_paths: Set of deploy-relative paths (e.g. ``maps/hallways.yaml``)
            for this config's generated files.
        namespace_prefix: Per-config prefix relative to the config mount root
            (e.g. ``<config-name>``).

    Returns:
        The value with file paths rewritten.
    """
    if isinstance(value, dict):
        return {k: _namespace_file_params(v, deploy_paths, namespace_prefix) for k, v in value.items()}
    if isinstance(value, list):
        return [_namespace_file_params(v, deploy_paths, namespace_prefix) for v in value]
    if isinstance(value, str) and value in deploy_paths:
        return f"{namespace_prefix}/{value}"
    return value


def build_job_parameter_documents(job, scenario_name):
    """Build scenario-parameter override documents for a packed job.

    Produces one YAML document per work item in the job. Each document
    overrides ``scenario_name``'s parameters for that config and sets the special
    ``_output_dir`` key to ``<config-name>/<run_number>`` so scenario_execution
    writes the item's results into robovast's per-config/run layout. File-valued
    parameters are namespaced under ``<config-name>/`` (relative to the config
    mount root) to keep multiple configs' files from colliding in a single job.

    Args:
        job: A :class:`~robovast.execution.packer.JobSpec`.
        scenario_name: The scenario name to override (top-level key, matching
            the single-config ``scenario.config`` wrapping).

    Returns:
        list[dict]: One override document per work item, ready to dump as a
        multi-document YAML for ``--scenario-parameter-file``.
    """
    documents = []
    for item in job.items:
        config_data = item.config
        config_name = config_data.get("name", "")
        config = config_data.get("config") or {}
        config_dict = convert_dataclasses_to_dict(copy.deepcopy(config))

        deploy_paths = {rel for rel, _ in config_data.get("_config_files", [])}
        # Prefix is relative to the config mount root: scenarios resolve file
        # params against their own directory (which *is* that mount root), so an
        # absolute "/config/..." here would double to "/config/config/...".
        namespace_prefix = config_name
        namespaced = _namespace_file_params(config_dict, deploy_paths, namespace_prefix)

        # _output_dir is consumed by scenario_execution to place this item's
        # results; relative paths resolve under -o/--output-dir.
        namespaced["_output_dir"] = f"{config_name}/{item.run_number}"
        documents.append({scenario_name: namespaced})
    return documents


def dump_multi_document_yaml(documents) -> str:
    """Serialise a list of dicts as a multi-document YAML string (``---`` separated)."""
    return yaml.dump_all(documents, default_flow_style=False, sort_keys=False)


# Filename of the per-campaign job-link manifest written into ``_transient/``.
JOB_LINKS_MANIFEST = "job_links.yaml"


def job_artifact_rel(index, job_prefix="") -> str:
    """Path of job *index*'s artifact dir under ``_jobs/`` (no leading ``_jobs/``).

    Nested ``<prefix>/job-<idx>`` when the run is batched, else flat ``job-<idx>``.
    Both backends lay ``_jobs/`` out this way, so this is the one definition of that
    layout — the local runner, the cluster runner, and every reader resolve through it.
    """
    return f"{job_prefix}/job-{index}" if job_prefix else f"job-{index}"


def build_job_links(jobs, job_prefix="") -> dict:
    """Map each work item's ``job`` link to its job's artifact directory.

    For a packed job ``N`` running config ``C`` at run ``R``, the work item's
    result dir is ``C/R`` and the job-level artifacts (sysinfo, logs, resource
    monitor) live in ``_jobs[/<prefix>]/job-N``. This returns a ``{link: target}``
    mapping where the link is ``C/R/job`` and the target is that dir relative to the
    link's directory, so a user can ``cd C/R/job`` to reach the job's artifacts.

    Args:
        jobs: An iterable of :class:`~robovast.execution.packer.JobSpec`.
        job_prefix: Batch namespace (e.g. ``"batch-3"``) when runs are executed in
            batches; empty for the flat single-batch layout. Must match the prefix the
            runner actually writes under, or the manifest points at a dir that
            never exists.

    Returns:
        dict[str, str]: ``{"<config>/<run>/job": "../../_jobs[/<prefix>]/job-<idx>"}``.
    """
    links = {}
    for job in jobs:
        target = f"../../_jobs/{job_artifact_rel(job.index, job_prefix)}"
        for item in job.items:
            links[f"{item.config_name}/{item.run_number}/job"] = target
    return links


def write_job_links_manifest(transient_dir, jobs, job_prefix="", *, base=None) -> None:
    """Write the ``job_links.yaml`` manifest (link → relative target) for *jobs*.

    No-op when there are no links (e.g. single-config jobs have no ``_jobs``
    split). The manifest is plain data, so it survives an S3 round-trip and is
    consumed where results are materialised (locally and in the share archiver).

    *base* is what this manifest must keep: the links the campaign already has, from
    :func:`read_job_links`. **The manifest is campaign-level while it is written per batch**,
    so a batch that publishes only its own links leaves every earlier batch's runs
    unresolvable — no ``run_log``, no ``resource_usage``, reported only as "no job_links
    entry". Pass it whenever more batches may follow.

    It is accumulated **only when** *job_prefix* namespaces the target, and that is not a
    detail. Unprefixed, a target is ``_jobs/job-<idx>``, an index meaningful only within the
    call that assigned it: re-running a campaign, or packing it differently, moves ``cfg/1``
    from ``job-1`` to ``job-0``, and keeping the older entry would aim a run at another run's
    artifacts. Prefixed (``_jobs/batch-3/job-0``) it is stable for the life of the campaign,
    and accumulating is then not optional but required. So an unprefixed write replaces,
    which is also what a single-batch campaign — one call, the default — has always done.

    Within a prefixed accumulation an entry whose target would *change* raises: one run's
    artifacts cannot live in two jobs, and picking a winner silently is how a run ends up
    resolving to another batch's log.
    """
    links = dict(base or {}) if job_prefix else {}
    for link, target in build_job_links(jobs, job_prefix).items():
        previous = links.get(link)
        if previous is not None and previous != target:
            raise ValueError(
                f"conflicting {JOB_LINKS_MANIFEST} entry for {link!r}: "
                f"already {previous!r}, now {target!r}")
        links[link] = target
    if not links:
        return
    os.makedirs(transient_dir, exist_ok=True)
    with open(os.path.join(transient_dir, JOB_LINKS_MANIFEST), "w") as f:
        yaml.dump(links, f, default_flow_style=False, sort_keys=True)


def read_job_links(campaign_dir) -> dict:
    """Load a campaign's ``{link: target}`` job-link manifest ({} when absent)."""
    manifest = os.path.join(campaign_dir, "_transient", JOB_LINKS_MANIFEST)
    if not os.path.isfile(manifest):
        return {}
    with open(manifest) as f:
        return yaml.safe_load(f) or {}


def job_artifact_dir(campaign_dir, job_name) -> str:
    """Resolve ``<config>/<run>``'s job-artifact dir (logs, sysinfo, resource monitor).

    Those artifacts are written per JOB under ``_jobs/``, never into the run dir, so
    ``<config>/<run>/logs/`` stays empty and reading there yields a silently blank log.

    Resolution goes through the manifest, not the ``job`` symlink: the manifest is
    written before the first job starts, while the symlink is only created once a job
    finishes, so a RUNNING job resolves through the manifest alone. The symlink stays
    the user-facing affordance (``cd C/R/job``); the manifest is the machine-readable
    source of truth, and both come from :func:`build_job_links`.

    Args:
        campaign_dir: The campaign's results directory.
        job_name: ``"<config>/<run>"``.

    Returns:
        str: Path to the job's artifact directory, relative to *campaign_dir*'s root
        in the same sense *campaign_dir* itself is.

    Raises:
        FileNotFoundError: When the campaign has no manifest entry for *job_name* —
            the job's artifacts are unlocatable, which must not be reported as
            "no output".
    """
    target = read_job_links(campaign_dir).get(f"{job_name}/job")
    if not target:
        raise FileNotFoundError(
            f"no {JOB_LINKS_MANIFEST} entry for {job_name!r} in {campaign_dir!r}")
    return os.path.normpath(os.path.join(campaign_dir, job_name, target))


def create_job_links(campaign_dir) -> int:
    """Create the ``job`` symlinks described by a campaign's link manifest.

    Reads ``<campaign_dir>/_transient/job_links.yaml`` and creates each
    ``<config>/<run>/job`` relative symlink pointing at its job's artifact dir.
    Idempotent: an existing ``job`` entry is replaced. Missing manifest is a
    no-op (single-config campaigns have none). Returns the number of links
    created.
    """
    links = read_job_links(campaign_dir)
    created = 0
    for link_rel, target in links.items():
        link_path = os.path.join(campaign_dir, link_rel)
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        # Replace any existing entry so re-runs are idempotent.
        if os.path.islink(link_path) or os.path.exists(link_path):
            try:
                os.remove(link_path)
            except OSError:
                pass
        os.symlink(target, link_path)
        created += 1
    return created


def generate_execution_yaml_script(runs, execution_params=None, output_dir_var="${RESULTS_DIR}",
                                   role_images=None):
    """Generate shell script code to create execution.yaml with ISO formatted timestamp.

    Args:
        runs: Number of runs
        execution_params: Dictionary containing execution parameters (run_as_user, env, etc.)
        output_dir_var: Shell variable name for the output directory (default: ${RESULTS_DIR})
        role_images: ``{role: image}`` for every container role this run starts, from the
            campaign's ``ContainerPlan``. Recorded as ``images`` plus a per-role
            ``image_revisions``, which is the same contract the cluster lane writes -- see
            below for why a single campaign-level image is not enough.

    Returns:
        String containing shell script code to create execution.yaml
    """
    if execution_params is None:
        execution_params = {}
    role_images = role_images or {}

    script = f'echo "Creating execution.yaml..."\n'
    script += f'EXECUTION_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")\n'
    # `| tr -d` + `|| true`, not `|| echo unknown`: for an image it does not have, `docker
    # inspect` prints an empty line on stdout *and* exits non-zero, so the old form captured
    # "\nunknown" -- a newline inside a YAML scalar, which made the whole file unparseable
    # rather than merely unknown. Empty now means "could not inspect", defaulted below.
    script += (f'IMAGE_REVISION=$(docker inspect --format=\'{{{{.Id}}}}\' "${{DOCKER_IMAGE}}" '
               f'2>/dev/null | tr -d "[:space:]" || true)\n')
    # One digest per ROLE, not just the campaign's. "The campaign's image" stopped describing
    # a run once the simulator, the system under test and the scenario got their own
    # containers, and anything attributing an artifact to the bytes that produced it has to
    # name the role: the run view compiles its geometry from the world the capture names, and
    # that world -- with the exporter that reads it -- lives in the SIMULATION image. Without
    # this the reader fell back to the campaign image and ran the exporter in a container that
    # had neither, which surfaced only as an exit 127 from a docker command.
    # Deduplicated by image because roles commonly share one, and `docker inspect` is a
    # process each.
    for i, image in enumerate(sorted(set(role_images.values()))):
        script += (f'ROLE_REVISION_{i}=$(docker inspect --format=\'{{{{.Id}}}}\' '
                   f'"{image}" 2>/dev/null | tr -d "[:space:]" || true)\n')
    script += f'mkdir -p "{output_dir_var}/_execution"\n'
    script += f'cat > "{output_dir_var}/_execution/execution.yaml" << EOF\n'
    script += "execution_time: '${EXECUTION_TIME}'\n"
    script += f'robovast_version: {get_app_version()}\n'
    script += f'runs: {runs}\n'
    script += f'execution_type: local\n'
    # The image that actually ran, not the .vast's raw entry: for a `build:<tag>` project the raw
    # entry is symbolic, and postprocessing re-runs its container from this file -- `docker run
    # build:<tag>` finds no such image, which surfaces as a bogus "compat version <missing>".
    script += "image: '${DOCKER_IMAGE}'\n"
    script += 'image_revision: ${IMAGE_REVISION:-unknown}\n'
    if role_images:
        slot = {image: i for i, image in enumerate(sorted(set(role_images.values())))}
        script += 'images:\n'
        for role, image in sorted(role_images.items()):
            script += f"  {role}: '{image}'\n"
        # Written by the shell, one `if` per role, so a role whose image could not be
        # inspected is OMITTED rather than recorded as "unknown". A recorded non-answer would
        # satisfy the reader's first source and defeat the point of recording at all.
        script += 'EOF\n'
        script += f'echo "image_revisions:" >> "{output_dir_var}/_execution/execution.yaml"\n'
        # `if`, not `[ ... ] && echo`: the latter exits non-zero when the test fails, which
        # would abort the run under `set -e` for the entirely normal case of one
        # uninspectable image.
        for role, image in sorted(role_images.items()):
            var = f'ROLE_REVISION_{slot[image]}'
            script += (f'if [ -n "${{{var}}}" ]; then '
                       f'echo "  {role}: ${{{var}}}" '
                       f'>> "{output_dir_var}/_execution/execution.yaml"; fi\n')
        script += f'cat >> "{output_dir_var}/_execution/execution.yaml" << EOF\n'
    # Local executions have no cluster information attached
    script += 'cluster_info: {}\n'

    # Add run_as_user if provided
    run_as_user = execution_params.get('run_as_user')
    if run_as_user is not None:
        script += f'run_as_user: {run_as_user}\n'

    # Add env if provided
    env = execution_params.get('env')
    if env:
        script += 'env:\n'
        for env_item in env:
            if isinstance(env_item, dict):
                for key, value in env_item.items():
                    # Escape special characters for heredoc
                    escaped_value = str(value).replace('"', '\\"').replace('$', '\\$') if value is not None else ""
                    script += f'  {key}: "{escaped_value}"\n'

    script += 'EOF\n'
    script += f'echo ""\n\n'
    return script


def _get_image_revision(image: str) -> str:
    """Return the local docker image ID for *image*, or ``'unknown'`` on failure."""
    if not image:
        return 'unknown'
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--format={{.Id}}', image],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            rev = result.stdout.strip()
            return rev if rev else 'unknown'
    except FileNotFoundError:
        pass
    return 'unknown'


def create_execution_yaml(runs, output_dir, execution_params=None, context=None,
                          image_digest=None, image_digests=None):
    """Create execution.yaml file with ISO formatted timestamp.

    Args:
        runs: Number of runs to include in execution.yaml
        output_dir: Directory where execution.yaml will be created
        execution_params: Dictionary containing execution parameters (run_as_user, env, etc.)
        context: Kubernetes context name to use. ``None`` uses the active context.
        image_digest: The immutable ``repo@sha256:…`` the run pods actually used, when
            known (see ``KubernetesBackend._capture_image_digest``). Recorded as
            ``image_revision`` so a floating ``:latest`` is pinned to the exact image the
            runs ran — and postprocessing reuses it (``campaign_execution_image``). Falls
            back to the local docker image id (``unknown`` off-cluster) when None.
    """
    if execution_params is None:
        execution_params = {}

    execution_dir = os.path.join(output_dir, "_execution")
    os.makedirs(execution_dir, exist_ok=True)
    execution_yaml_path = os.path.join(execution_dir, "execution.yaml")
    execution_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 'image' stays the container the scenario ran in -- postprocessing and the store
    # read it as "the campaign's image". 'images' records every container, because with
    # a simulator or a system under test in its own container that single field no
    # longer describes what ran.
    # Imported here, not at module scope: this module is kept free of ``config``
    # imports to avoid a cycle (see BUILD_IMAGE_PREFIX above, duplicated for the same
    # reason). One name is not worth duplicating a third time.
    from robovast.common.config import SCENARIO_CONTAINER  # pylint: disable=import-outside-toplevel
    containers = execution_params.get('containers') or {}
    images = {name: (block or {}).get('image')
              for name, block in containers.items() if (block or {}).get('image')}
    image = images.get(SCENARIO_CONTAINER)
    execution_data = {
        'execution_time': execution_time,
        'robovast_version': get_app_version(),
        'runs': runs,
        'execution_type': 'cluster',
        'image': image,
        'images': images,
        'image_revision': image_digest or _get_image_revision(image),
    }
    # One digest per container, because "the campaign's image" stopped being a single
    # fact. `image_revision` is the scenario container's; anything asking which bytes
    # produced a particular artifact has to name the role. The run view's geometry is the
    # case in hand: it is compiled from the world the capture names, and that world and
    # its exporter live in the SIMULATION image, not the scenario one.
    if image_digests:
        execution_data['image_revisions'] = dict(image_digests)

    # Add run_as_user if provided
    run_as_user = execution_params.get('run_as_user')
    if run_as_user is not None:
        execution_data['run_as_user'] = run_as_user

    # Add env if provided
    env = execution_params.get('env')
    if env:
        # Convert list of dicts to a single dict for cleaner YAML output
        env_dict = {}
        for env_item in env:
            if isinstance(env_item, dict):
                env_dict.update(env_item)
        if env_dict:
            execution_data['env'] = env_dict

    # Attach cluster information (node count, labels, and cluster config)
    cluster_info = _get_cluster_info(context=context)
    if cluster_info is not None:
        execution_data['cluster_info'] = cluster_info

    with open(execution_yaml_path, 'w') as f:
        yaml.dump(execution_data, f, default_flow_style=False, sort_keys=False)

    logger.debug(f"Created execution.yaml with timestamp: {execution_time}")
