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

import fnmatch
import logging
import os
import shlex
import sys
import tempfile

import yaml

from robovast.client.project_config import get_project_config
from robovast.common import (COMPAT_VERSION, COMPAT_VERSION_LABEL, MIN_IMAGE_COMPAT,
                             generate_execution_yaml_script, get_execution_env_variables,
                             load_config, plan_containers, prepare_campaign_configs,
                             scenario_env)
from robovast.common.common import get_scenario_parameters
from robovast.common.config import (SCENARIO_CONTAINER, SIMULATION_CONTAINER,
                                    declared_per_run_seconds)
from robovast.common.config_generation import generate_scenario_variations
from robovast.common.execution import (_apply_local_parameter_overrides,
                                       build_job_parameter_documents, dump_multi_document_yaml,
                                       job_artifact_rel, local_parameter_overrides, read_job_links,
                                       resolve_robovast_image, sidecar_backend_env,
                                       write_job_links_manifest)
from robovast.common.quantity import to_cores
from robovast.common.simulators import SIM_OVERRIDES_MOUNT, sim_job_overlay
from robovast.execution.packer import build_jobs

logger = logging.getLogger(__name__)


def initialize_local_execution(config, output_dir, runs, feedback_callback=logging.debug,
                               skip_resource_allocation=True, log_tree=False, debug=False,
                               gui=False):
    """Initialize common setup for local execution commands.

    Performs all common setup steps including:
    - Loading project and execution configuration
    - Validating config exists
    - Creating output directory
    - Preparing run configuration files
    - Generating config path

    Args:
        config: The config name to execute
        output_dir: Directory where output files will be written, if none a temporary directory is created
        runs: Number of runs per config
        feedback_callback: Function to call for feedback messages (e.g., print or click.echo)

    Raises:
        SystemExit: If initialization fails
    """
    if output_dir:
        logger.info(f"Initializing local execution environment in '{output_dir}'...")
    else:
        logger.info("Initializing local execution environment in temporary directory...")
    # Load configuration
    project_config = get_project_config()
    config_path = project_config.config_path
    logger.debug(f"Loading config from: {config_path}")
    execution_parameters = load_config(config_path, "execution")
    docker_image = resolve_robovast_image(
        config_image=_declared_scenario_image(execution_parameters))
    pre_command = execution_parameters.get("pre_command")
    post_command = execution_parameters.get("post_command")
    results_dir = project_config.results_dir

    # Use execution_parameters value if runs is not provided
    if runs is None:
        if "runs" not in execution_parameters:
            logger.error("Number of runs not specified in command or config")
            feedback_callback("Error: Number of runs not specified in command or config.")
            sys.exit(1)
        else:
            runs = execution_parameters["runs"]

    logger.debug(f"Using Docker image: {docker_image}")

    # Generate and filter configs
    logger.debug("Generating scenario variations")
    temp_dir = tempfile.TemporaryDirectory(prefix="robovast_execution_")
    campaign_data = generate_scenario_variations(
        variation_file=config_path,
        progress_update_callback=None,
        output_dir=temp_dir.name
    )

    if not campaign_data["configs"]:
        logger.error("No configs found in vast-file")
        feedback_callback("Error: No configs found in vast-file.", file=sys.stderr)
        sys.exit(1)

    # Filter to configs matching the pattern if requested
    if config:
        matched = [cfg for cfg in campaign_data["configs"] if fnmatch.fnmatch(cfg['name'], config)]

        if not matched:
            feedback_callback(f"Error: No configs matched pattern '{config}'.", file=sys.stderr)
            feedback_callback("Available configs:")
            for cfg in campaign_data["configs"]:
                feedback_callback(f"  - {cfg['name']}")
            sys.exit(1)

        campaign_data["configs"] = matched

    logger.debug(f"Preparing {len(campaign_data['configs'])} configs from {config_path}...")
    logger.debug(f"Output directory: {output_dir}")

    # Create temp directory for run() or use output_dir for prepare_run()
    temp_path = None
    if not output_dir:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_local_", delete=False)
        logger.debug(f"Using temporary directory for config files: {temp_path.name}")
        logger.debug(f"Temp path: {temp_path.name}")
        config_dir = temp_path.name
    else:
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:  # pylint: disable=broad-except
            feedback_callback(f"Error creating output directory: {e}", file=sys.stderr)
            sys.exit(1)
        config_dir = output_dir

    try:
        config_path_result = os.path.join(config_dir, "out_template")
        prepare_campaign_configs(config_path_result, campaign_data, gui=gui)
        logger.debug(f"Config path: {config_path_result}")
    except Exception as e:  # pylint: disable=broad-except
        feedback_callback(f"Error preparing run configs: {e}", file=sys.stderr)
        sys.exit(1)

    logger.debug(f"Configuration files prepared in: {config_dir}")

    # Check if run_as_user differs from local user and warn about potential permission issues
    execution_params = campaign_data.get("execution", {})
    run_as_user = execution_params.get("run_as_user", 1000)
    host_uid = os.getuid()
    if run_as_user != host_uid:
        logger.warning(f"Container will run as UID {run_as_user}, but host user is UID {host_uid}. "
                       f"This may cause permission issues with bind-mounted directories. "
                       f"Consider setting 'run_as_user: {host_uid}' in your .vast config for local testing.")

    generate_compose_run_script(runs, campaign_data, config_path_result, pre_command, post_command,
                                docker_image, results_dir, os.path.join(config_dir, "run.sh"),
                                skip_resource_allocation=skip_resource_allocation,
                                log_tree=log_tree, debug=debug, gui=gui)
    return os.path.join(config_dir, "run.sh")


RUN_SCRIPT_HEADER = """#!/usr/bin/env bash

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)

# Default Docker image
DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"
USE_GUI=true
START_ONLY=false
CAMPAIGN_ID="CAMPAIGN_NAME_PLACEHOLDER-$(date +%Y-%m-%d-%H%M%S%N | cut -c1-19)"
RESULTS_DIR=

# Variables to track cleanup and interrupt state
CLEANUP_DONE=0
COMPOSE_PID=
LOG_PID=
SIGINT_COUNT=0
ABORT_ON_FAILURE=false
OVERALL_EXIT_CODE=0
SCENARIO_EXECUTION_PARAMS=""

# Cleanup function
cleanup() {
    if [ $CLEANUP_DONE -eq 1 ]; then
        return
    fi
    CLEANUP_DONE=1

    if [ -n "$LOG_PID" ]; then
        kill "$LOG_PID" 2>/dev/null || true
        LOG_PID=
    fi

    echo ""
    echo "Cleaning up containers..."
    if [ -n "$CURRENT_COMPOSE_FILE" ]; then
        docker compose -f "$CURRENT_COMPOSE_FILE" down --volumes --timeout 5 2>/dev/null || true
    fi
}

# SIGINT handler: first press triggers graceful shutdown; subsequent presses force-kill
handle_sigint() {
    SIGINT_COUNT=$((SIGINT_COUNT + 1))
    if [ $SIGINT_COUNT -eq 1 ]; then
        echo ""
        echo "Stopping... (press Ctrl+C again to force exit)"
        if [ -n "$COMPOSE_PID" ]; then
            kill -TERM "$COMPOSE_PID" 2>/dev/null || true
        fi
        # Keep streaming logs while containers shut down (skip if a live log
        # follower is already running, e.g. the secondary-container path).
        if [ -z "$LOG_PID" ] && [ -n "$CURRENT_COMPOSE_FILE" ]; then
            docker compose -f "$CURRENT_COMPOSE_FILE" logs --follow 2>/dev/null &
            LOG_PID=$!
        fi
    else
        echo ""
        echo "Force exiting..."
        if [ -n "$LOG_PID" ]; then
            kill "$LOG_PID" 2>/dev/null || true
            LOG_PID=
        fi
        if [ -n "$COMPOSE_PID" ]; then
            disown "$COMPOSE_PID" 2>/dev/null || true
            kill -KILL "$COMPOSE_PID" 2>/dev/null || true
        fi
        if [ -n "$CURRENT_COMPOSE_FILE" ]; then
            docker compose -f "$CURRENT_COMPOSE_FILE" kill 2>/dev/null || true
        fi
        cleanup
        exit 130
    fi
}

# Set up signal handlers
trap 'handle_sigint' SIGINT
trap 'cleanup; exit 130' SIGTERM

# Show help
show_help() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run the robovast Docker containers.

OPTIONS:
    --image IMAGE       Use a custom Docker image (default: ghcr.io/cps-test-lab/robovast:latest)
    --no-gui            Disable host GUI support
    --results-dir DIR   Override the results parent dir (a campaign-id subdir is created under it)
    --campaign-dir DIR  Use DIR verbatim as the campaign root (no campaign-id subdir; used by the controller)
    --start-only        Start the robovast container with a shell, skipping the entrypoint script
    --abort-on-failure  Stop execution after the first failed run config
    --log-tree, -t      Pass --live-tree to scenario execution
    --debug, -d         Pass --debug to scenario execution
    -h, --help          Show this help message
EOF
}

# Parse command-line arguments
while [ $# -gt 0 ]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        --image)
            DOCKER_IMAGE="$2"
            shift 2
            ;;
        --no-gui)
            USE_GUI=false
            shift
            ;;
        --start-only)
            START_ONLY=true
            shift
            ;;
        --abort-on-failure)
            ABORT_ON_FAILURE=true
            shift
            ;;
        --log-tree | -t)
            SCENARIO_EXECUTION_PARAMS="$SCENARIO_EXECUTION_PARAMS -t"
            shift
            ;;
        --debug | -d)
            SCENARIO_EXECUTION_PARAMS="$SCENARIO_EXECUTION_PARAMS -d"
            shift
            ;;
        --results-dir)
            if [[ "$2" != /* ]]; then
                echo "Error: --results-dir must be an absolute path (starting with /)"
                exit 1
            fi
            echo "Overriding results directory to: $2"
            RESULTS_DIR="$2/${CAMPAIGN_ID}"
            shift 2
            ;;
        --campaign-dir)
            if [[ "$2" != /* ]]; then
                echo "Error: --campaign-dir must be an absolute path (starting with /)"
                exit 1
            fi
            echo "Using campaign directory: $2"
            RESULTS_DIR="$2"
            shift 2
            ;;
        -*)
            # An unknown *option* is an error, not something to walk past. This case
            # used to `break`, which silently discarded it — and everything after it,
            # since parsing stopped there. That is how `--network-host` survived as a
            # documented, forwarded, entirely dead flag: nothing ever said it was
            # unknown. Non-option arguments still end parsing (the case below).
            echo "Error: unknown option '$1'" >&2
            echo "" >&2
            show_help >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
done

# GUI setup. The X socket, /dev/dri and DISPLAY are wired into the compose file itself;
# what is left for the script is letting containers on this host talk to the X server, and
# picking the GL path. A failed grant is reported rather than swallowed -- silencing it
# turned "the X server refused the container" into a run that started fine and drew
# nothing.
if [ "$USE_GUI" = true ]; then
    if ! xhost +local: > /dev/null 2>&1; then
        echo "WARNING: xhost +local: failed; the X server may refuse the container" >&2
    fi
    if [ -e /dev/dri ]; then
        export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-0}"
    else
        export LIBGL_ALWAYS_SOFTWARE=1
    fi
fi

mkdir -p "${RESULTS_DIR}"

# Mirror all run.sh + docker compose output into the campaign's controller.log
# so the web UI "Show log" (which streams that file) shows the container/compose
# output alongside the controller narrative. tee still forwards to the original
# stdout, preserving console / _control/logs output. Skipped for --start-only,
# which is an interactive TTY shell (a pipe would break its terminal handling).
if [ "$START_ONLY" != true ]; then
    mkdir -p "${RESULTS_DIR}/_execution"
    exec > >(tee -a "${RESULTS_DIR}/_execution/controller.log") 2>&1
fi

# Pull image if not available locally.
#
# A failed pull is FATAL and says so here. It used to fall through to the protocol check, which
# could then only report the secondary symptom -- "the image reports no version" -- while the
# actual fact was that the image does not exist or is not reachable. For a re-run of an archived
# campaign that distinction is the whole answer: an image that cannot be obtained needs its
# recorded build refs, not a protocol conversation.
if ! docker image inspect "$DOCKER_IMAGE" > /dev/null 2>&1; then
    echo "Docker image '$DOCKER_IMAGE' not found locally. Downloading..."
    if ! docker pull "$DOCKER_IMAGE"; then
        echo ""
        echo "ERROR: could not obtain the image '$DOCKER_IMAGE'."
        echo "  It is neither present locally nor pullable from its registry."
        echo ""
        echo "  If this is a re-run of an archived campaign, the image it recorded is gone:"
        echo "  see _execution/execution.yaml for what it was built from, and rebuild from"
        echo "  that revision. Pulling a newer tag would run different code."
        exit 1
    fi
    echo ""
fi

# Container protocol check. The label first: `docker inspect` reads it without starting
# anything, where the legacy file costs a whole container to read one integer. The file is
# still consulted, because an image built before the label carries only that -- and those are
# exactly the campaigns worth re-running.
IMAGE_COMPAT=$(docker inspect --format '{{index .Config.Labels "@@COMPAT_LABEL@@"}}' "$DOCKER_IMAGE" 2>/dev/null || echo "")
COMPAT_SOURCE="label"
if [ -z "$IMAGE_COMPAT" ] || [ "$IMAGE_COMPAT" = "<no value>" ]; then
    IMAGE_COMPAT=$(docker run --rm --entrypoint cat "$DOCKER_IMAGE" /etc/robovast_compat_version 2>/dev/null || echo "")
    COMPAT_SOURCE="file"
fi
# A RANGE, not equality. Equality meant the first bump orphaned every published image, so a
# campaign pinning one by digest could never be re-run -- refusing the case this exists for.
if [ -z "$IMAGE_COMPAT" ]; then
    echo "ERROR: cannot determine the container protocol version of '$DOCKER_IMAGE'."
    echo "  This host speaks @@MIN_IMAGE_COMPAT@@..@@COMPAT_VERSION@@."
    echo "  The image reports neither the @@COMPAT_LABEL@@ label nor /etc/robovast_compat_version,"
    echo "  so it is either not a robovast image or predates both markers."
    exit 1
elif [ "$IMAGE_COMPAT" -gt "@@COMPAT_VERSION@@" ]; then
    echo "ERROR: '$DOCKER_IMAGE' speaks container protocol $IMAGE_COMPAT (from its $COMPAT_SOURCE),"
    echo "  but this host speaks @@MIN_IMAGE_COMPAT@@..@@COMPAT_VERSION@@."
    echo "  The image is NEWER than this robovast -- upgrade robovast, do not rebuild the image."
    exit 1
elif [ "$IMAGE_COMPAT" -lt "@@MIN_IMAGE_COMPAT@@" ]; then
    echo "ERROR: '$DOCKER_IMAGE' speaks container protocol $IMAGE_COMPAT (from its $COMPAT_SOURCE),"
    echo "  but this host speaks @@MIN_IMAGE_COMPAT@@..@@COMPAT_VERSION@@ and no longer supports it."
    echo ""
    echo "  Either check out the robovast revision the campaign recorded"
    echo "  (_execution/execution.yaml: robovast_revision) and run it there, or rebuild the"
    echo "  image from that revision. Do NOT pull a newer image: a re-run needs the bytes the"
    echo "  campaign recorded, not today's."
    exit 1
fi
"""


def _compose_resources_block(cpu, memory, indent="    "):
    """Return deploy.resources.limits YAML lines for a service, or empty string if none specified.

    ``cpu`` is normalized to a plain number of cores because Compose's ``cpus`` is a decimal
    count, **not** a Kubernetes quantity: ``cpus: '500m'`` is a Compose validation error, so
    passing the millicore spelling through verbatim would make a ``.vast`` that validates and
    runs on the cluster fail on the local lane. ``memory`` needs no such treatment — Compose
    takes the same suffixed form (``2Gi``) that Kubernetes does.
    """
    if not cpu and not memory:
        return ""
    lines = [
        f"{indent}deploy:",
        f"{indent}  resources:",
        f"{indent}    limits:",
    ]
    if cpu:
        lines.append(f"{indent}      cpus: '{_compose_cpus(cpu)}'")
    if memory:
        lines.append(f"{indent}      memory: {memory}")
    return "\n".join(lines)


def _compose_cpus(cpu) -> str:
    """A cpu declaration as Compose's decimal core count (``"500m"`` -> ``"0.5"``).

    An unparseable value is passed through unchanged rather than dropped: the config layer
    already rejects those, and if one ever reaches here, Compose's own error naming the bad
    value beats this silently removing the limit.
    """
    cores = to_cores(cpu)
    if cores is None:
        return str(cpu)
    return str(int(cores)) if float(cores).is_integer() else str(cores)


def _build_packed_compose_yaml(
    docker_image,
    out_path,
    results_dir_var,
    job,
    param_file_rel,
    run_files,
    env_vars,
    pre_command,
    post_command,
    uid,
    gid,
    main_cpu,
    main_memory,
    main_gpu,
    plan,
    use_gui_block,
    skip_resource_allocation=True,
    scenario_execution_params='',
    scenario_env_vars=None,
    execution=None,
    job_prefix='',
    sim_command=None,
    sim_overrides_rel=None,
    sim_env=None,
):
    """Build docker-compose YAML for one job.

    ``/out`` is the campaign root (scenario_execution writes per-config
    ``_output_dir`` subdirs), a single multi-document parameter file is mounted
    at ``/config/scenario.params.yaml``, and each config's generated files are
    mounted under ``/config/<config-name>/`` to avoid collisions. Used for both
    single-config (one config per job) and packed (several configs per job) runs.

    *plan* is the campaign's :class:`~robovast.common.containers.ContainerPlan`: its
    main container runs the scenario, and every other one becomes a sidecar sharing the
    main container's network and IPC namespaces.

    A sidecar with no ``command`` runs ``secondary_entrypoint.sh``, i.e. a
    ``scenario_execution_server`` the scenario drives over the ``/ipc/<name>`` socket
    with ``remote()``. One that declares a command runs that instead -- how a simulator
    or a stack that RoboVAST does not drive is started. Either way it receives the same
    packed param file and namespaced per-config file mounts as the main container, so
    file-valued parameters resolve identically on both sides.
    """

    def quote(s):
        return s.replace('"', '\\"')

    # SCENARIO_FILE both names the mount and reaches the entrypoint, so it is read
    # from the one derived env rather than passed a second time alongside it.
    scenario_env_vars = dict(scenario_env_vars or {})
    scenario_file_name = scenario_env_vars.get('SCENARIO_FILE', 'scenario.osc')

    sidecars = plan.sidecars
    has_secondaries = bool(sidecars)

    def _packed_config_mounts():
        """Volume mount lines shared by the main and secondary containers."""
        yield f'      - "{quote(results_dir_var)}/{param_file_rel}:/config/scenario.params.yaml:ro"'
        # The simulation channel's per-job document, beside the scenario channel's. In
        # every container for the same reason that one is: which container reads it is the
        # backend's business, and the stepped shape has only one.
        if sim_overrides_rel:
            yield (f'      - "{quote(results_dir_var)}/{sim_overrides_rel}'
                   f':{SIM_OVERRIDES_MOUNT}:ro"')
        yield f'      - "{quote(results_dir_var)}/_config/{scenario_file_name}:/config/{scenario_file_name}:ro"'
        for run_file in run_files:
            yield f'      - "{quote(results_dir_var)}/_config/{run_file}:/config/{run_file}:ro"'
        # Per-config generated files, namespaced under /config/<config-name>/
        for config_data in (it.config for it in job.items):
            config_name = config_data.get("name", "")
            for deploy_rel, _src in config_data.get("_config_files", []):
                yield (
                    f'      - "{quote(results_dir_var)}/{config_name}/_config/{deploy_rel}'
                    f':/config/{config_name}/{deploy_rel}:ro"'
                )
        if has_secondaries:
            yield '      - shared_tmp:/tmp'
            yield '      - shared_ipc:/ipc'

    # Environment selecting the packed parameter file + per-scenario output.
    # /out is the campaign root; per-config results go to /out/<config>/<run> via
    # each document's _output_dir (SCENARIO_OUTPUT_DIR), while this job's job-level
    # artifacts (sysinfo, resource monitor, logs) go to a per-job subdir so they
    # don't collide across jobs. ``job_prefix`` (e.g. "batch-3") namespaces those
    # job dirs so multiple batches sharing one campaign root don't collide.
    job_artifact_dir = f"/out/_jobs/{job_artifact_rel(job.index, job_prefix)}"
    packed_env_lines = [
        "      - SCENARIO_PARAMETER_FILE=/config/scenario.params.yaml",
        "      - OUTPUT_RESULT_PER_SCENARIO=true",
        f"      - OUTPUT_DIR={job_artifact_dir}",
        "      - SCENARIO_OUTPUT_DIR=/out",
    ]
    # Where *this run's* results land, when the job is exactly one run. Neither variable above names
    # it: /out is the campaign root and OUTPUT_DIR is per-job. A process the scenario only launched
    # (a simulator started by a ROS launch file) otherwise has nowhere correct to write a per-run
    # artifact. Omitted for a packed job, where one variable cannot serve several work items.
    if len(getattr(job, "items", None) or []) == 1:
        item = job.items[0]
        packed_env_lines.append(f"      - RUN_OUTPUT_DIR=/out/{item.config_name}/{item.run_number}")

    lines = []
    lines.append("services:")
    lines.append("  robovast:")
    lines.append(f"    image: ${{DOCKER_IMAGE}}")
    lines.append(f"    container_name: robovast")
    lines.append(f"    init: true")
    if main_gpu:
        lines.append("    runtime: nvidia")
    if has_secondaries:
        lines.append("    ipc: shareable")
    shm_size = (execution or {}).get('shm_size')
    # The sidecars join this container's IPC namespace below, so they share its /dev/shm --
    # Docker's default 64 MB unless this says otherwise. Set on the main container only,
    # because that is the one whose namespace the others inherit. Same `execution.shm_size`
    # as the cluster lane, so one .vast means the same thing on both.
    if shm_size:
        lines.append(f"    shm_size: {shm_size}")

    lines.append("    volumes:")
    lines.append(f'      - "{quote(out_path)}:/out"')
    lines.append(f'      - "{quote(results_dir_var)}/_transient/entrypoint.sh:/config/entrypoint.sh:ro"')
    lines.append(f'      - "{quote(results_dir_var)}/_transient/collect_sysinfo.py:/config/collect_sysinfo.py:ro"')
    lines.append(f'      - "{quote(results_dir_var)}/_transient/monitor_resources.py:/config/monitor_resources.py:ro"')
    lines.extend(_packed_config_mounts())
    if use_gui_block:
        lines.append("      - /tmp/.X11-unix:/tmp/.X11-unix:rw")
        lines.append("      - /dev/dri:/dev/dri")

    lines.append("    environment:")
    # With no simulation sidecar the simulator runs in this container (the stepped shape),
    # so the job's resolved simulator environment belongs here instead.
    main_env = dict(env_vars)
    if sim_env and not any(sc.name == SIMULATION_CONTAINER for sc in sidecars):
        main_env.update(sim_env)
    for key, value in main_env.items():
        lines.append(f"      - {key}={value}")
    if pre_command:
        lines.append(f'      - PRE_COMMAND={pre_command}')
    if post_command:
        lines.append(f'      - POST_COMMAND={post_command}')
    lines.append("      - AVAILABLE_CPUS=${AVAILABLE_CPUS}")
    lines.append("      - AVAILABLE_MEM=${AVAILABLE_MEM}")
    for key, value in scenario_env_vars.items():
        lines.append(f"      - {key}={value}")
    lines.extend(packed_env_lines)
    if scenario_execution_params:
        lines.append(f"      - SCENARIO_EXECUTION_PARAMETERS={scenario_execution_params}")
    if use_gui_block:
        lines.append("      - DISPLAY=${DISPLAY:-:0}")
        lines.append("      - LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-0}")
    if main_gpu:
        lines.append("      - QT_X11_NO_MITSHM=1")
        lines.append("      - NVIDIA_VISIBLE_DEVICES=all")
        lines.append("      - NVIDIA_DRIVER_CAPABILITIES=all")

    if not skip_resource_allocation:
        res = _compose_resources_block(main_cpu, main_memory)
        if res:
            lines.append(res)

    lines.append(f"    user: \"{uid}:{gid}\"")
    lines.append("    stop_grace_period: 60s")
    lines.append("    command: ${ROBOVAST_COMMAND}")
    lines.append("    tty: ${ROBOVAST_TTY}")
    lines.append("    stdin_open: ${ROBOVAST_STDIN_OPEN}")

    for sc in sidecars:
        sc_name = sc.name
        sc_cpu = sc.resources.get('cpu')
        sc_memory = sc.resources.get('memory')
        sc_gpu = sc.resources.get('gpu')

        lines.append(f"  {sc_name}:")
        lines.append(f"    image: {sc.image}")
        lines.append(f"    container_name: {sc_name}")
        if sc_gpu:
            lines.append("    runtime: nvidia")
        lines.append(f"    network_mode: service:robovast")
        lines.append(f"    ipc: service:robovast")
        lines.append(f"    depends_on:")
        lines.append(f"      - robovast")
        lines.append("    volumes:")
        lines.append(f'      - "{quote(out_path)}:/out"')
        lines.append(f'      - "{quote(results_dir_var)}/_transient/secondary_entrypoint.sh:/config/secondary_entrypoint.sh:ro"')
        lines.append(f'      - "{quote(results_dir_var)}/_transient/collect_sysinfo.py:/config/collect_sysinfo.py:ro"')
        lines.append(f'      - "{quote(results_dir_var)}/_transient/monitor_resources.py:/config/monitor_resources.py:ro"')
        lines.extend(_packed_config_mounts())
        if use_gui_block:
            lines.append("      - /tmp/.X11-unix:/tmp/.X11-unix:rw")
            lines.append("      - /dev/dri:/dev/dri")
        lines.append("    environment:")
        lines.append(f"      - CONTAINER_NAME={sc_name}")
        lines.append(f"      - SCENARIO_FILE={scenario_file_name}")
        # The backend's own env, for the container the backend describes. scenario_env
        # puts it on the main container, which is only right when the simulator IS the
        # main container (the stepped shape); in the ROS shape it is this sidecar.
        # The job's own resolved values win over the campaign default: a world belongs to
        # a configuration, and this sidecar is running one.
        sc_backend_env = dict(sidecar_backend_env(execution or {}, sc_name))
        if sc_name == SIMULATION_CONTAINER:
            sc_backend_env.update(sim_env or {})
        for key, value in sc_backend_env.items():
            lines.append(f"      - {key}={value}")
        # The container's own command, for secondary_entrypoint.sh to exec once it has
        # set the environment up. Deliberately NOT named SECONDARY_COMMAND: that name is
        # already a host-shell variable compose substitutes into `command:` above, and
        # one name meaning two things across the substitution boundary is a trap.
        # The simulator's command is the one thing in the plan that is per-configuration:
        # it names the world, and a world belongs to a configuration. Every job in this
        # campaign runs the same container from the same image with the same resources --
        # only this one argv differs, and only because the packer guarantees a job's items
        # agree on it.
        sc_cmd = (sim_command if (sc_name == SIMULATION_CONTAINER and sim_command)
                  else sc.command)
        if sc_cmd:
            lines.append("      - ROBOVAST_CONTAINER_COMMAND="
                         + shlex.join(list(sc_cmd)))
        lines.extend(packed_env_lines)
        for key, value in env_vars.items():
            lines.append(f"      - {key}={value}")
        if use_gui_block:
            lines.append("      - DISPLAY=${DISPLAY:-:0}")
            lines.append("      - LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-0}")
        if sc_gpu:
            lines.append("      - QT_X11_NO_MITSHM=1")
            lines.append("      - NVIDIA_VISIBLE_DEVICES=all")
            lines.append("      - NVIDIA_DRIVER_CAPABILITIES=all")

        if not skip_resource_allocation:
            sc_res = _compose_resources_block(sc_cpu, sc_memory)
            if sc_res:
                lines.append(sc_res)

        lines.append(f"    user: \"{uid}:{gid}\"")
        lines.append("    stop_signal: SIGINT")
        lines.append("    stop_grace_period: 5s")
        # Always the entrypoint, never the bare command: it sources the ROS overlay,
        # tees stdout into the job's log dir and starts the resource monitor. A
        # container whose command was exec'd directly got none of those -- and a
        # colcon-built plugin is only importable once /opt/ros and /ws/install are
        # sourced, so a simulator started that way failed on an unregistered plugin
        # with no log to say so. The command travels by env instead, which is what
        # lets one entrypoint serve both kinds of sidecar.
        lines.append("    command: ${SECONDARY_COMMAND}")
        lines.append("    tty: ${ROBOVAST_TTY}")
        lines.append("    stdin_open: ${ROBOVAST_STDIN_OPEN}")

    if has_secondaries:
        lines.append("")
        lines.append("volumes:")
        lines.append("  shared_tmp:")
        lines.append("  shared_ipc:")
        lines.append("    driver: local")
        lines.append("    driver_opts:")
        lines.append("      type: tmpfs")
        lines.append("      device: tmpfs")
        lines.append('      o: "mode=0777"')

    return "\n".join(lines)


def _timeout_prefix(step_timeout_s):
    """``timeout`` prefix enforcing a step's wall-clock limit, or ``""`` for none.

    SIGTERM first, so ``docker compose`` stops its containers the same way Ctrl+C makes
    it — a scenario gets its shutdown, and ``test.xml`` still lands for a run that was
    cut off. ``--kill-after`` is the backstop for a compose that ignores the term.

    A timed-out step exits 124, which the step's existing failure handling already treats
    as a failed run: a truncated batch must report failed, not pass as a shorter success.
    """
    if not step_timeout_s:
        return ""
    return f'timeout --signal=TERM --kill-after=30s {int(step_timeout_s)} '


def _emit_compose_step(compose_file, compose_yaml, idx, total, label, has_secondaries, noun,
                       post_down="", step_timeout_s=None):
    """Return the shell text that writes, runs, waits on and tears down one compose stack.

    Shared by the single-config and packed (multi-config) code paths. ``idx`` is
    1-based; the final step (``idx == total``) emits the overall summary/exit.
    Each call corresponds to exactly one ``docker compose up``/``down`` cycle:
    every container in the stack starts once and stays up until the step
    completes (no per-parameter-set restarts).

    ``post_down`` is shell injected right after this step's ``docker compose
    down`` and before the failure/summary handling — so it runs for every step,
    including the last (whose summary block ends in ``exit``). The packed path
    uses it to create this job's artifact links per job (Ctrl+C-safe).
    """
    s = f'CURRENT_COMPOSE_FILE="{compose_file}"\n'
    s += 'export DOCKER_IMAGE RESULTS_DIR AVAILABLE_CPUS AVAILABLE_MEM LIBGL_ALWAYS_SOFTWARE ROBOVAST_COMMAND SECONDARY_COMMAND ROBOVAST_TTY ROBOVAST_STDIN_OPEN SCENARIO_EXECUTION_PARAMS\n'
    s += f'cat > "{compose_file}" << \'COMPOSE_EOF\'\n'
    s += compose_yaml + '\n'
    s += 'COMPOSE_EOF\n\n'

    # Run compose in background with SIGINT ignored in the child before exec.
    # Go programs (docker compose) preserve SIG_IGN across exec, so Ctrl+C
    # from the terminal does not reach docker compose directly. The compose
    # process stays in the same session so it keeps its controlling terminal,
    # which is required for proper container stop output and graceful shutdown.
    # Explicit signals (SIGTERM/SIGKILL) are sent by handle_sigint as needed.
    compose_bg = (
        f'( trap \'\' SIGINT; export COMPOSE_MENU=false;'
        f' {_timeout_prefix(step_timeout_s)}docker compose -f "{compose_file}" up'
        f' --abort-on-container-exit'
        f' --exit-code-from robovast'
        f' 2> >(grep -v "Aborting on container exit" >&2)'
        f') &\n'
    )
    compose_wait = 'COMPOSE_PID=$!\n'
    compose_wait += (
        'wait "$COMPOSE_PID" 2>/dev/null\n'
        'WAIT_CODE=$?\n'
        'while [ "$WAIT_CODE" -ge 128 ] && kill -0 "$COMPOSE_PID" 2>/dev/null; do\n'
        '    wait "$COMPOSE_PID" 2>/dev/null\n'
        '    WAIT_CODE=$?\n'
        'done\n'
        'COMPOSE_PID=\n'
    )
    compose_wait += (
        'EXIT_CODE=$WAIT_CODE\n'
        'if [ "$SIGINT_COUNT" -gt 0 ]; then\n'
        '    cleanup\n'
        '    exit 130\n'
        'fi\n'
    )
    # Secondary-container path: start the stack detached and block on the MAIN
    # ``robovast`` container only (via ``docker wait``) instead of aborting the
    # whole stack when *any* container exits. A secondary's watchdog can fire
    # during scenario_execution's teardown — e.g. after a failed scenario, while
    # the main container is busy stopping processes and deleting entities — and
    # exit first. With ``--abort-on-container-exit`` that secondary exit would
    # SIGTERM the main container before scenario_execution writes ``test.xml``,
    # so a failed run produces no result at all. Waiting on ``robovast`` lets it
    # finish teardown and emit a (failed) ``test.xml``; the secondaries are then
    # stopped by the ``down`` that follows this step. Logs are streamed by a
    # background follower since detached ``up -d`` is silent.
    compose_secondary = (
        f'( trap \'\' SIGINT; export COMPOSE_MENU=false;'
        f' docker compose -f "{compose_file}" up -d )\n'
        'UP_CODE=$?\n'
        'if [ "$UP_CODE" -ne 0 ]; then\n'
        '    EXIT_CODE=$UP_CODE\n'
        'else\n'
        f'    docker compose -f "{compose_file}" logs --follow 2>/dev/null &\n'
        '    LOG_PID=$!\n'
        '    WAIT_OUT="$(mktemp)"\n'
        # The limit lands on the wait, not the detached ``up``: with secondaries the
        # step's duration is however long the main container runs. Timing out leaves the
        # containers up, and this step's ``down`` below removes them.
        f'    ( trap \'\' SIGINT; {_timeout_prefix(step_timeout_s)}'
        'docker wait robovast > "$WAIT_OUT" 2>/dev/null ) &\n'
        '    COMPOSE_PID=$!\n'
        '    wait "$COMPOSE_PID" 2>/dev/null\n'
        '    WAIT_CODE=$?\n'
        '    while [ "$WAIT_CODE" -ge 128 ] && kill -0 "$COMPOSE_PID" 2>/dev/null; do\n'
        '        wait "$COMPOSE_PID" 2>/dev/null\n'
        '        WAIT_CODE=$?\n'
        '    done\n'
        '    COMPOSE_PID=\n'
        '    if [ -n "$LOG_PID" ]; then\n'
        '        kill "$LOG_PID" 2>/dev/null || true\n'
        '        LOG_PID=\n'
        '    fi\n'
        '    EXIT_CODE="$(cat "$WAIT_OUT" 2>/dev/null)"\n'
        '    rm -f "$WAIT_OUT"\n'
        '    [ -z "$EXIT_CODE" ] && EXIT_CODE=$WAIT_CODE\n'
        '    if [ "$SIGINT_COUNT" -gt 0 ]; then\n'
        '        cleanup\n'
        '        exit 130\n'
        '    fi\n'
        'fi\n'
    )
    if has_secondaries:
        s += compose_secondary
    else:
        s += f'if [ "$START_ONLY" = true ]; then\n'
        s += f'    docker compose -f "{compose_file}" run --rm --entrypoint /bin/bash robovast\n'
        s += '    EXIT_CODE=$?\n'
        s += f'else\n'
        for line in compose_bg.splitlines(keepends=True):
            s += f'    {line}'
        for line in compose_wait.splitlines(keepends=True):
            s += f'    {line}'
        s += f'fi\n'

    s += f'docker compose -f "{compose_file}" down --volumes --timeout 5 2>/dev/null || true\n'
    if post_down:
        s += post_down
    if idx < total:
        s += 'if [ $EXIT_CODE -ne 0 ]; then\n'
        s += f'    echo "Warning: {label} failed with exit code $EXIT_CODE"\n'
        s += '    OVERALL_EXIT_CODE=$EXIT_CODE\n'
        s += '    if [ "$ABORT_ON_FAILURE" = true ]; then\n'
        s += '        cleanup\n'
        s += '        exit $EXIT_CODE\n'
        s += '    fi\n'
        s += 'fi\n\n'
    else:
        s += 'if [ $EXIT_CODE -ne 0 ]; then\n'
        s += '    OVERALL_EXIT_CODE=$EXIT_CODE\n'
        s += 'fi\n'
        s += 'if [ $OVERALL_EXIT_CODE -eq 0 ]; then\n'
        s += f'    echo ""\n'
        s += f'    echo "{"=" * 60}"\n'
        s += f'    echo "All {total} {noun} completed successfully!"\n'
        s += f'    echo "{"=" * 60}"\n'
        s += 'else\n'
        s += f'    echo "Error: One or more of {total} {noun} failed (last exit code: $OVERALL_EXIT_CODE)"\n'
        s += 'fi\n'
        s += 'cleanup\n'
        s += 'exit $OVERALL_EXIT_CODE\n'
    return s


def _declared_scenario_image(execution_params: dict):
    """The image declared for the container the scenario runs in, if any."""
    containers = execution_params.get("containers") or {}
    return (containers.get(SCENARIO_CONTAINER) or {}).get("image")


def generate_compose_run_script(runs, campaign_data, config_path_result, pre_command, post_command,
                                docker_image, results_dir, output_script_path,
                                skip_resource_allocation=False, log_tree=False, debug=False,
                                job_prefix='', gui=False, built_images=None):
    """Generate a shell script to run Docker Compose stacks sequentially.

    Args:
        runs: Number of runs per config
        campaign_data: Dictionary containing configs and run files
        config_path_result: Path to the config results directory
        pre_command: Command to run before execution (optional)
        post_command: Command to run after execution (optional)
        docker_image: Image for the container the scenario runs in (an explicit
            ``--image`` / the resolved default). Sidecars come from the plan.
        built_images: Concrete refs for containers whose image was built, by container
            name. A container absent from here runs its declared image verbatim.
        results_dir: Directory where results are stored
        output_script_path: Path where the script should be written
        gui: Whether this run has the host display wired in. Selects the
            ``execution.local.gui`` parameter overrides; defaults to off so a caller that
            does not thread it through stages the headless defaults.
    """
    run_files = campaign_data.get("_run_files", [])

    if not campaign_data["configs"]:
        raise ValueError("At least one config configuration is required")

    execution_params = campaign_data.get("execution", {})
    run_as_user = execution_params.get("run_as_user")
    if run_as_user is None:
        run_as_user = os.getuid()
    uid = run_as_user
    gid = run_as_user

    # Every container this campaign runs, with its image already resolved. One map,
    # shared with the cluster lane and exec_in_container -- a second lookup here would be
    # free to disagree with what the pod actually starts.
    plan = plan_containers(execution_params, images=built_images,
                           explicit_main=docker_image)
    resources = plan.main.resources or {}
    main_cpu = resources.get("cpu")
    main_memory = resources.get("memory")
    main_gpu = resources.get("gpu")

    # Per-step wall-clock limit, or None for no limit.
    #
    # ``execution.timeout`` is a *per-run* figure and a step may pack several runs, so it
    # is scaled by ``runs_per_job`` — the same arithmetic the cluster applies to a Job's
    # ``activeDeadlineSeconds`` (see ``kubernetes_backend``), so one key means one thing
    # on both lanes.
    #
    # ``declared_per_run_seconds``, not ``per_run_deadline_seconds``: the latter falls
    # back to an hour, and inventing a limit where the user set none is a different
    # decision from enforcing one they did set. An undeclared timeout stays unbounded
    # here, exactly as before.
    declared_timeout = declared_per_run_seconds(execution_params)
    runs_per_job = int(execution_params.get("runs_per_job") or 1)
    step_timeout_s = declared_timeout * runs_per_job if declared_timeout else None

    script = RUN_SCRIPT_HEADER.replace(
        'DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"',
        f'DOCKER_IMAGE="{docker_image}"', 1
    ).replace(
        'CAMPAIGN_NAME_PLACEHOLDER',
        (campaign_data.get('metadata') or {}).get('name', 'campaign'), 1
    ).replace(
        'RESULTS_DIR=',
        f'RESULTS_DIR="{results_dir}/${{CAMPAIGN_ID}}"', 1
    ).replace(
        '@@COMPAT_VERSION@@', str(COMPAT_VERSION),
    ).replace(
        '@@MIN_IMAGE_COMPAT@@', str(MIN_IMAGE_COMPAT),
    ).replace(
        '@@COMPAT_LABEL@@', COMPAT_VERSION_LABEL,
    )

    if step_timeout_s:
        script += (f'echo "Per-step limit: {step_timeout_s}s '
                   f'(execution.timeout {declared_timeout}s x runs_per_job {runs_per_job})."\n')
        script += 'echo ""\n'

    # Copy out_template to results dir
    script += f'echo "Copying out_template contents to ${{RESULTS_DIR}}..."\n'
    script += f'cp -r "${{SCRIPT_DIR}}/out_template/"* "${{RESULTS_DIR}}/"\n'
    script += f'echo ""\n\n'

    # The plan, not the declared `containers` mapping: it already resolved each image and
    # already folded the roles a stepped simulator collapses, so what is recorded is what
    # this script actually starts.
    script += generate_execution_yaml_script(
        runs, execution_params=campaign_data.get("execution", {}),
        role_images={role: plan.by_name(role).image
                     for role in plan.roles
                     if plan.by_name(role).image})

    scenario_env_vars = scenario_env(campaign_data)
    _static_params = " ".join(p for p, enabled in [("-t", log_tree), ("-d", debug)] if enabled)
    scenario_execution_params = _static_params if _static_params else "${SCENARIO_EXECUTION_PARAMS}"

    def _emit_preamble(banner, mkdir_dirs):
        """Per-step banner, output-dir creation, resource vars and command selection."""
        s = f'\necho ""\n'
        s += f'echo "{"=" * 60}"\n'
        s += f'echo "{banner}"\n'
        s += f'echo "{"=" * 60}"\n'
        s += f'echo ""\n\n'
        for d in mkdir_dirs:
            s += f'mkdir -p "{d}/logs"\n'
            s += f'chmod -R 777 "{d}"\n'
        # Set AVAILABLE_CPUS/MEM from configured resources. CPU is normalized to cores for the
        # same reason the compose block is, plus one of its own: this lands in sysinfo.yaml and
        # then in the ``runs.available_cpus`` REAL column, where "500m" would be stored as text
        # in a numeric column and every comparison against it would quietly stop working.
        s += f'AVAILABLE_CPUS="{_compose_cpus(main_cpu) if main_cpu else ""}"\n'
        if main_memory:
            s += f'AVAILABLE_MEM="{main_memory}"\n'
        else:
            s += "AVAILABLE_MEM=\"$(awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo)\"\n"
        s += '\n# Determine command and interactive settings based on START_ONLY\n'
        s += 'if [ "$START_ONLY" = true ]; then\n'
        s += '    ROBOVAST_COMMAND="/bin/bash"\n'
        s += '    SECONDARY_COMMAND="/bin/bash"\n'
        s += '    ROBOVAST_TTY="true"\n'
        s += '    ROBOVAST_STDIN_OPEN="true"\n'
        s += 'else\n'
        s += '    # Use string format for command to be consistent with variable substitution\n'
        s += '    ROBOVAST_COMMAND="/bin/bash /config/entrypoint.sh"\n'
        s += '    SECONDARY_COMMAND="/bin/bash /config/secondary_entrypoint.sh"\n'
        s += '    ROBOVAST_TTY="false"\n'
        s += '    ROBOVAST_STDIN_OPEN="false"\n'
        s += 'fi\n\n'
        return s

    has_secondaries = bool(plan.sidecars)

    # Every run goes through the job mechanism: runs_per_job=1 yields one job
    # per (config, run), >1 packs several configs per job. Both produce the same
    # layout — results in <config>/<run>/ and job artifacts in _jobs/job-N/ with
    # a <config>/<run>/job symlink.
    # campaign_data["scenario_file"] is already resolved relative to the vast's
    # location during config generation (dirname(vast) + execution.scenario_file),
    # so it is the path relative to cwd -- do not re-prepend dirname(vast).
    scenario_path = campaign_data["scenario_file"]
    scenario_params_by_name = get_scenario_parameters(scenario_path)
    scenario_name = next(iter(scenario_params_by_name.keys()))

    # Local-only scenario-parameter overrides (execution.local.parameter_overrides, plus the
    # execution.local.gui block when this run has a display): applied to every packed job document
    # below so they reach the container -- the packed params.yaml is what the local run mounts.
    # ``scenario.config`` also carries them, but the local run uses the job documents.
    local_param_overrides = local_parameter_overrides(campaign_data, gui=gui)
    valid_param_names = [
        p.get("name") for p in scenario_params_by_name.get(scenario_name, [])
        if isinstance(p, dict) and "name" in p
    ]

    jobs = build_jobs(campaign_data["configs"], runs, execution_params)
    os.makedirs(os.path.join(config_path_result, "_transient"), exist_ok=True)
    # Canonical record of the per-job artifact links (also used by the cluster
    # share archiver). Local runs create the links inline per job below so a
    # Ctrl+C only loses the job active at cancel time.
    #
    # Accumulated, not replaced: a search campaign calls this once per batch, and the
    # manifest is campaign-level -- writing only this batch's links leaves every earlier
    # batch's runs with no locatable job artifacts, hence no run_log and no resource_usage.
    write_job_links_manifest(os.path.join(config_path_result, "_transient"), jobs,
                             job_prefix, base=read_job_links(config_path_result))
    total = len(jobs)
    for idx, job in enumerate(jobs, 1):
        documents = build_job_parameter_documents(job, scenario_name)
        if local_param_overrides:
            for doc in documents:
                _apply_local_parameter_overrides(
                    doc[scenario_name], local_param_overrides, valid_param_names,
                    scenario_name, scenario_path)
        param_rel = f"_transient/job-{job.index}.params.yaml"
        with open(os.path.join(config_path_result, param_rel), 'w') as f:
            f.write(dump_multi_document_yaml(documents))

        # The simulation channel's per-job file, written beside the scenario channel's.
        # Single-document, not multi: the packer groups by `sim_key`, so a job's items
        # agree on their simulator settings by construction and `job.items[0]` speaks for
        # all of them. (The scenario file is multi-document because its items do NOT
        # agree -- that is the whole point of packing them.)
        sim_overlay = sim_job_overlay(
            campaign_data.get("execution") or {},
            job.items[0].config.get("sim") or {},
            os.path.dirname(campaign_data.get("vast") or ""))
        sim_rel = None
        if sim_overlay["document"]:
            sim_rel = f"_transient/job-{job.index}.sim.yaml"
            with open(os.path.join(config_path_result, sim_rel), 'w') as f:
                yaml.dump(sim_overlay["document"], f, default_flow_style=False,
                          sort_keys=False)

        compose_file = f"/tmp/robovast_compose_job-{job.index}.yml"
        mkdir_dirs = [
            os.path.join("${RESULTS_DIR}", it.config_name, str(it.run_number))
            for it in job.items
        ]
        names = ", ".join(job.config_names)
        script += _emit_preamble(
            f"{idx}/{total} Executing job {job.index} "
            f"({len(job.items)} parameter set(s): {names})", mkdir_dirs)
        env_vars = get_execution_env_variables(
            0, "", campaign_data.get('execution', {}).get('env'))
        compose_yaml = _build_packed_compose_yaml(
            docker_image=docker_image, out_path="${RESULTS_DIR}", results_dir_var="${RESULTS_DIR}",
            job=job, param_file_rel=param_rel, run_files=run_files, env_vars=env_vars,
            pre_command=pre_command, post_command=post_command, uid=uid, gid=gid,
            main_cpu=main_cpu, main_memory=main_memory, main_gpu=main_gpu,
            plan=plan, use_gui_block=True,
            skip_resource_allocation=skip_resource_allocation,
            scenario_execution_params=scenario_execution_params,
            scenario_env_vars=scenario_env_vars,
            execution=campaign_data.get('execution', {}),
            job_prefix=job_prefix,
            sim_command=sim_overlay["command"],
            sim_overrides_rel=sim_rel,
            sim_env=sim_overlay["env"],
        )
        # Create this job's artifact links right after it finishes (injected
        # after the compose `down`, before the step's summary/exit), so a
        # Ctrl+C only loses the links for the job active at cancel time.
        # Each <config>/<run>/job points at this job's _jobs[/<prefix>]/job-N dir.
        job_rel = f"_jobs/{job_artifact_rel(job.index, job_prefix)}"
        link_cmds = "".join(
            f'ln -sfn "{os.path.relpath("/" + job_rel, "/" + os.path.join(it.config_name, str(it.run_number)))}" '
            f'"{os.path.join("${RESULTS_DIR}", it.config_name, str(it.run_number))}/job"\n'
            for it in job.items
        )
        script += _emit_compose_step(
            compose_file, compose_yaml, idx, total,
            f"Job {idx}/{total}", has_secondaries, "job(s)", post_down=link_cmds,
            step_timeout_s=step_timeout_s)

    try:
        with open(output_script_path, 'w') as f:
            f.write(script)
        os.chmod(output_script_path, 0o755)
        logger.debug(f"Generated Docker Compose run script: {output_script_path}")
    except Exception as e:  # pylint: disable=broad-except
        logger.error(f"Error writing Docker Compose run script: {e}")
        raise
