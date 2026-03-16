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
from .config_identifier import (compute_config_identifier, hash_file_content,
                                hash_run_files)

# Compatibility version between host robovast code and the container image.
# Bump this integer when the contract between host scripts and the container
# changes (e.g. new required package, ROS distro change, script interface
# change).  The same value must appear in the Dockerfile as
# /etc/robovast_compat_version.
COMPAT_VERSION = 1


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
        str or None: The cpuManagerPolicy value (e.g. "static", "none"), or None on failure
    """
    try:
        response = k8s_client.connect_get_node_proxy_with_path(node_name, "configz")
        data = json.loads(response)
        kubelet_config = data.get("kubeletconfig", {})
        return kubelet_config.get("cpuManagerPolicy")
    except Exception as exc:
        logger.debug("Could not retrieve kubelet configz for node %s: %s", node_name, exc)
        return "none"


def _get_cluster_info(context=None):
    """Collect basic cluster information for cluster executions.

    Args:
        context: Kubernetes context name to use. ``None`` uses the active context.

    Returns a dictionary with node_count, node_labels, cpu_manager_policy and
    cluster_config (loaded from the .robovast_cluster_config flag file) when
    available.  Failures are logged and result in partial or empty data rather
    than errors.
    """
    cluster_info = {}

    # Load cluster config info from flag file if available
    try:
        from robovast.execution.cluster_execution.cluster_setup import \
            get_cluster_config_flag_path  # pylint: disable=import-outside-toplevel

        try:
            flag_path = get_cluster_config_flag_path()
            if os.path.exists(flag_path):
                with open(flag_path, "r", encoding="utf-8") as f:
                    config_data = yaml.safe_load(f) or {}
                cluster_info["cluster_config"] = config_data
        except Exception as exc:  # pragma: no cover - best-effort, non-fatal
            logger.warning("Failed to load cluster config from flag file: %s", exc)
    except Exception:  # pragma: no cover - import may fail in non-cluster contexts
        # If cluster modules are not available, silently skip cluster_config
        pass

    # Collect node information via Kubernetes Python API
    node_count = None
    node_labels = {}
    cpu_manager_policies = {}
    try:
        from kubernetes import \
            client as k8s_client_lib  # pylint: disable=import-outside-toplevel
        from kubernetes import \
            config as k8s_config  # pylint: disable=import-outside-toplevel

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config(context=context)

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
                if policy is not None:
                    cpu_manager_policies[name] = policy

        # Warn if any node does not have the Static CPU Manager policy enabled
        if cpu_manager_policies:
            logger.debug(f"Static CPU Manager policy is enabled on {
                         len(cpu_manager_policies)} node(s): {', '.join(cpu_manager_policies.keys())}")

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
    second for concurrent-run disambiguation) are recognised.
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


_LOCAL_INIT_BLOCK = "eval $(fixuid -q)"

_CLUSTER_INIT_BLOCK = ""

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
echo "[s3-upload] Mirroring /out/ to mystore/${S3_BUCKET}/${S3_PREFIX}/..."
mc mirror /out/ "mystore/${S3_BUCKET}/${S3_PREFIX}/"
echo "[s3-upload] Mirror complete. Re-tagging executable files..."
# Re-tag executable files with x-amz-meta-executable metadata
_exec_count=0
find /out/ -type f -executable | while IFS= read -r f; do
    rel="${f#/out/}"
    mc cp --attr "x-amz-meta-executable=yes" "mystore/${S3_BUCKET}/${S3_PREFIX}/${rel}" "mystore/${S3_BUCKET}/${S3_PREFIX}/${rel}" --quiet
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


def prepare_campaign_configs(out_dir, campaign_data, cluster=False):
    # Create the output directory structure
    logger.debug(f"Campaign Configs: {pformat(campaign_data)}")
    os.makedirs(out_dir, exist_ok=True)

    campaign_config_dir = os.path.join(out_dir, "_config")
    os.makedirs(campaign_config_dir, exist_ok=True)

    campaign_transient_dir = os.path.join(out_dir, "_transient")
    os.makedirs(campaign_transient_dir, exist_ok=True)

    # Inject the run-mode-specific post-run block into the shared entrypoint template
    entrypoint_src = str(files('robovast.execution.data').joinpath('entrypoint.sh'))
    with open(entrypoint_src, 'r', encoding='utf-8') as f:
        entrypoint_content = f.read()
    init_block = _CLUSTER_INIT_BLOCK if cluster else _LOCAL_INIT_BLOCK
    entrypoint_content = entrypoint_content.replace('# @@INIT_BLOCK@@', init_block)
    post_run_block = _CLUSTER_POST_RUN_BLOCK if cluster else _LOCAL_POST_RUN_BLOCK
    entrypoint_content = entrypoint_content.replace('    # @@POST_RUN_BLOCK@@', post_run_block)
    entrypoint_dst = os.path.join(campaign_transient_dir, "entrypoint.sh")
    with open(entrypoint_dst, 'w', encoding='utf-8') as f:
        f.write(entrypoint_content)

    # Copy secondary_entrypoint.sh into _transient/ (with init block replacement)
    secondary_entrypoint_src = str(files('robovast.execution.data').joinpath('secondary_entrypoint.sh'))
    with open(secondary_entrypoint_src, 'r', encoding='utf-8') as f:
        secondary_entrypoint_content = f.read()
    secondary_entrypoint_content = secondary_entrypoint_content.replace('# @@INIT_BLOCK@@', init_block)
    secondary_entrypoint_dst = os.path.join(campaign_transient_dir, "secondary_entrypoint.sh")
    with open(secondary_entrypoint_dst, 'w', encoding='utf-8') as f:
        f.write(secondary_entrypoint_content)

    # Copy collect_sysinfo.py into _transient/
    collect_sysinfo_src = str(files('robovast.execution.data').joinpath('collect_sysinfo.py'))
    collect_sysinfo_dst = os.path.join(campaign_transient_dir, "collect_sysinfo.py")
    shutil.copy2(collect_sysinfo_src, collect_sysinfo_dst)

    # Copy monitor_resources.py into _transient/
    monitor_resources_src = str(files('robovast.execution.data').joinpath('monitor_resources.py'))
    monitor_resources_dst = os.path.join(campaign_transient_dir, "monitor_resources.py")
    shutil.copy2(monitor_resources_src, monitor_resources_dst)

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
    scenario_file_path_for_hash = (
        campaign_data["scenario_file"]
        if os.path.isabs(campaign_data["scenario_file"])
        else os.path.join(vast_file_path, campaign_data["scenario_file"])
    )
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
    original_scenario_path = os.path.join(vast_file_path, campaign_data.get("scenario_file"))
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

    # Get local parameter overrides (only applied when not running on cluster)
    parameter_overrides = []
    if not cluster:
        local_config = campaign_data.get("execution", {}).get("local")
        if local_config is not None:
            if hasattr(local_config, 'parameter_overrides'):
                parameter_overrides = local_config.parameter_overrides or []
            elif isinstance(local_config, dict):
                parameter_overrides = local_config.get("parameter_overrides") or []

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
                    raise FileNotFoundError(f"Config file {src_path} does not exist.")
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


def generate_execution_yaml_script(runs, execution_params=None, output_dir_var="${RESULTS_DIR}"):
    """Generate shell script code to create execution.yaml with ISO formatted timestamp.

    Args:
        runs: Number of runs
        execution_params: Dictionary containing execution parameters (run_as_user, env, etc.)
        output_dir_var: Shell variable name for the output directory (default: ${RESULTS_DIR})

    Returns:
        String containing shell script code to create execution.yaml
    """
    if execution_params is None:
        execution_params = {}

    script = f'echo "Creating execution.yaml..."\n'
    script += f'EXECUTION_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")\n'
    script += f'IMAGE_REVISION=$(docker inspect --format=\'{{{{.Id}}}}\' "${{DOCKER_IMAGE}}" 2>/dev/null || echo "unknown")\n'
    script += f'mkdir -p "{output_dir_var}/_execution"\n'
    script += f'cat > "{output_dir_var}/_execution/execution.yaml" << EOF\n'
    script += 'execution_time: ${EXECUTION_TIME}\n'
    script += f'robovast_version: {get_app_version()}\n'
    script += f'runs: {runs}\n'
    script += f'execution_type: local\n'
    script += f'image: {execution_params.get("image")}\n'
    script += 'image_revision: ${IMAGE_REVISION}\n'
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


def create_execution_yaml(runs, output_dir, execution_params=None, context=None):
    """Create execution.yaml file with ISO formatted timestamp.

    Args:
        runs: Number of runs to include in execution.yaml
        output_dir: Directory where execution.yaml will be created
        execution_params: Dictionary containing execution parameters (run_as_user, env, etc.)
        context: Kubernetes context name to use. ``None`` uses the active context.
    """
    if execution_params is None:
        execution_params = {}

    execution_dir = os.path.join(output_dir, "_execution")
    os.makedirs(execution_dir, exist_ok=True)
    execution_yaml_path = os.path.join(execution_dir, "execution.yaml")
    execution_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    image = execution_params.get('image')
    execution_data = {
        'execution_time': execution_time,
        'robovast_version': get_app_version(),
        'runs': runs,
        'execution_type': 'cluster',
        'image': image,
        'image_revision': _get_image_revision(image),
    }

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
