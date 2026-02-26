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

import datetime
import json
import logging
import os
import shutil
from importlib.resources import files
from pprint import pformat

import yaml

from .common import convert_dataclasses_to_dict, get_scenario_parameters

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


def _get_cluster_info():
    """Collect basic cluster information for cluster executions.

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
            k8s_config.load_kube_config()

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


def get_run_id():
    return f"run-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"


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
    test_id = f"{config_name}-{run_num}"
    env_vars = {
        'TEST_ID': test_id,
        'ROS_LOG_DIR': '/out/logs',
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
    POST_COMMAND_PARAM=""
    if [ -n "${POST_COMMAND}" ]; then
        if [ -e "${POST_COMMAND}" ]; then
            POST_COMMAND_PARAM="--post-run ${POST_COMMAND}"
            log "Post-command set to: ${POST_COMMAND}"
        else
            log "ERROR: Post-command '${POST_COMMAND}' does not exist."
            exit 1
        fi
    fi"""

_CLUSTER_POST_RUN_BLOCK = """\
    # Build the S3 upload script; output is mirrored to the S3 bucket after the run
    S3_UPLOAD_SCRIPT="/tmp/s3_upload.sh"
    cat > "${S3_UPLOAD_SCRIPT}" << 'UPLOAD_EOF'
#!/bin/bash
set -e
mc alias set myminio "${S3_ENDPOINT}" "${S3_ACCESS_KEY}" "${S3_SECRET_KEY}" --quiet
mc mirror /out/ "myminio/${S3_BUCKET}/${S3_PREFIX}/"
UPLOAD_EOF
    chmod +x "${S3_UPLOAD_SCRIPT}"

    if [ -n "${POST_COMMAND}" ]; then
        if [ -e "${POST_COMMAND}" ]; then
            COMBINED_SCRIPT="/tmp/combined_post_run.sh"
            cat > "${COMBINED_SCRIPT}" << COMBINED_EOF
#!/bin/bash
set -e
source "${POST_COMMAND}"
"${S3_UPLOAD_SCRIPT}"
COMBINED_EOF
            chmod +x "${COMBINED_SCRIPT}"
            POST_COMMAND_PARAM="--post-run ${COMBINED_SCRIPT}"
            log "Post-command '${POST_COMMAND}' combined with S3 upload."
        else
            log "ERROR: Post-command '${POST_COMMAND}' does not exist."
            exit 1
        fi
    else
        POST_COMMAND_PARAM="--post-run ${S3_UPLOAD_SCRIPT}"
    fi"""


def prepare_run_configs(out_dir, run_data, cluster=False):
    # Create the output directory structure
    logger.debug(f"Run Configs: {pformat(run_data)}")
    os.makedirs(out_dir, exist_ok=True)

    # Inject the run-mode-specific post-run block into the shared entrypoint template
    entrypoint_src = str(files('robovast.execution.data').joinpath('entrypoint.sh'))
    with open(entrypoint_src, 'r', encoding='utf-8') as f:
        entrypoint_content = f.read()
    init_block = _CLUSTER_INIT_BLOCK if cluster else _LOCAL_INIT_BLOCK
    entrypoint_content = entrypoint_content.replace('# @@INIT_BLOCK@@', init_block)
    post_run_block = _CLUSTER_POST_RUN_BLOCK if cluster else _LOCAL_POST_RUN_BLOCK
    entrypoint_content = entrypoint_content.replace('    # @@POST_RUN_BLOCK@@', post_run_block)
    entrypoint_dst = os.path.join(out_dir, "entrypoint.sh")
    with open(entrypoint_dst, 'w', encoding='utf-8') as f:
        f.write(entrypoint_content)

    # Copy secondary_entrypoint.sh so secondary containers can use it (with init block replacement)
    secondary_entrypoint_src = str(files('robovast.execution.data').joinpath('secondary_entrypoint.sh'))
    with open(secondary_entrypoint_src, 'r', encoding='utf-8') as f:
        secondary_entrypoint_content = f.read()
    secondary_entrypoint_content = secondary_entrypoint_content.replace('# @@INIT_BLOCK@@', init_block)
    secondary_entrypoint_dst = os.path.join(out_dir, "secondary_entrypoint.sh")
    with open(secondary_entrypoint_dst, 'w', encoding='utf-8') as f:
        f.write(secondary_entrypoint_content)

    # Copy collect_sysinfo.py to the out directory so it can be mounted
    # into the container alongside entrypoint.sh for both local and cluster runs.
    collect_sysinfo_src = str(files('robovast.execution.data').joinpath('collect_sysinfo.py'))
    collect_sysinfo_dst = os.path.join(out_dir, "collect_sysinfo.py")
    shutil.copy2(collect_sysinfo_src, collect_sysinfo_dst)

    # Copy vast file to the out directory
    shutil.copy2(run_data["vast"], out_dir)

    run_config_dir = os.path.join(out_dir, "_config")
    os.makedirs(run_config_dir, exist_ok=True)

    # Save scenario variations as YAML in _config subdirectory
    scenario_variations_path = os.path.join(run_config_dir, "configurations.yaml")
    with open(scenario_variations_path, 'w') as f:
        yaml.dump(convert_dataclasses_to_dict(run_data), f, default_flow_style=False, sort_keys=False)
    logger.debug(f"Saved configurations to {scenario_variations_path}")

    vast_file_path = os.path.dirname(run_data["vast"])
    # copy scenario_file
    scenario_file_path = os.path.join(vast_file_path, run_data["scenario_file"])
    shutil.copy2(scenario_file_path, out_dir)

    # Copy test files
    for config_file in run_data.get("_test_files", []):
        src_path = os.path.join(vast_file_path, config_file)
        dst_path = os.path.join(run_config_dir, config_file)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)

    # get scenario name
    original_scenario_path = os.path.join(vast_file_path, run_data.get("scenario_file"))
    try:
        scenario_params = get_scenario_parameters(original_scenario_path)
        scenario_name = next(iter(scenario_params.keys()))

        if scenario_name is None:
            raise ValueError(f"Scenario name not found in {original_scenario_path}")
    except Exception as e:
        raise RuntimeError(f"Could not get scenario name from {original_scenario_path}: {e}") from e

    for config_data in run_data["configs"]:
        test_config_dir = os.path.join(out_dir, config_data.get("name"), "_config")

        # Copy config files
        if "_config_files" in config_data:
            for config_rel_path, config_path in config_data["_config_files"]:
                if not os.path.exists(config_path):
                    raise FileNotFoundError(f"Config file {config_path} does not exist.")
                src_path = config_path
                dst_path = os.path.join(test_config_dir, config_rel_path)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy2(src_path, dst_path)

        # Create config file if needed
        if "config" in config_data:
            config = config_data.get('config')
            if config is not None:
                wrapped_config_data = {scenario_name: config}
                dst_path = os.path.join(out_dir, config_data.get("name"), 'scenario.config')
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                with open(dst_path, 'w') as f:
                    converted_config_data = convert_dataclasses_to_dict(wrapped_config_data)
                    yaml.dump(converted_config_data, f, default_flow_style=False, sort_keys=False)


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
    script += f'cat > "{output_dir_var}/execution.yaml" << EOF\n'
    script += 'execution_time: ${EXECUTION_TIME}\n'
    script += f'runs: {runs}\n'
    script += f'execution_type: local\n'
    script += f'image: {execution_params.get("image")}\n'
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


def create_execution_yaml(runs, output_dir, execution_params=None):
    """Create execution.yaml file with ISO formatted timestamp.

    Args:
        runs: Number of runs to include in execution.yaml
        output_dir: Directory where execution.yaml will be created
        execution_params: Dictionary containing execution parameters (run_as_user, env, etc.)
    """
    if execution_params is None:
        execution_params = {}

    execution_yaml_path = os.path.join(output_dir, "execution.yaml")
    execution_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    execution_data = {
        'execution_time': execution_time,
        'runs': runs,
        'execution_type': 'cluster',
        'image': execution_params.get('image')
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
    cluster_info = _get_cluster_info()
    if cluster_info is not None:
        execution_data['cluster_info'] = cluster_info

    with open(execution_yaml_path, 'w') as f:
        yaml.dump(execution_data, f, default_flow_style=False, sort_keys=False)

    logger.debug(f"Created execution.yaml with timestamp: {execution_time}")
