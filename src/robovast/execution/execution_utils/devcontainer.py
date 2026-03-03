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

"""VSCode devcontainer generation for vast execution local setup-devcontainer."""

import glob
import json
import os
import shutil
import tempfile

from robovast.common import prepare_run_configs
from robovast.common.config import normalize_secondary_containers
from robovast.common.config_generation import generate_scenario_variations

# Prefix used for secondary container_name in docker compose so restart scripts
# can target them by a predictable name.
_SECONDARY_CONTAINER_PREFIX = "robovast-devcontainer-"


def prepare_devcontainer_config_dir(config_dir, project_config_path, config_name=None):
    """Assemble a /config-compatible directory for devcontainer use.

    Calls prepare_run_configs into a temp directory, then copies the generated
    system/config files into config_dir.  Test files (from test_files_filter)
    are NOT copied; their host paths are returned so the caller can add them as
    direct bind-mounts in docker-compose.yml.

    Args:
        config_dir: Destination directory (will be created / overwritten).
        project_config_path: Path to the .vast config file.
        config_name: Which config to use; defaults to the first one.

    Returns:
        (vast_dir, test_files, config_files, secondary_containers) where
        test_files and config_files are lists of relative paths (relative to
        the .vast file's directory) that should be mounted directly from the
        host as /config/{rel_path}, and secondary_containers is a list of
        normalized dicts (with 'name' key) for any secondary containers defined
        in the project execution config.
    """
    vast_dir = os.path.dirname(os.path.abspath(project_config_path))

    with tempfile.TemporaryDirectory(prefix="robovast_devcontainer_") as tmp:
        run_data, _ = generate_scenario_variations(
            variation_file=project_config_path,
            progress_update_callback=None,
            output_dir=tmp,
        )

        configs = run_data.get("configs", [])
        if not configs:
            raise ValueError("No configs found in .vast file")

        if config_name:
            cfg = next((c for c in configs if c["name"] == config_name), None)
            if cfg is None:
                available = [c["name"] for c in configs]
                raise ValueError(
                    f"Config '{config_name}' not found. Available: {', '.join(available)}"
                )
        else:
            cfg = configs[0]

        # Generate all template + config files into tmp/out_template
        out_template = os.path.join(tmp, "out_template")
        prepare_run_configs(out_template, run_data)

        os.makedirs(config_dir, exist_ok=True)

        # Copy system files that are always generated (not user-editable)
        for fname in ("entrypoint.sh", "secondary_entrypoint.sh", "collect_sysinfo.py"):
            src = os.path.join(out_template, fname)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(config_dir, fname))

        # Copy scenario .osc file (named scenario.osc in /config by convention)
        osc_files = glob.glob(os.path.join(out_template, "*.osc"))
        if osc_files:
            shutil.copy2(osc_files[0], os.path.join(config_dir, "scenario.osc"))

        # Copy scenario.config for the chosen config
        scenario_config_src = os.path.join(out_template, cfg["name"], "scenario.config")
        if os.path.exists(scenario_config_src):
            shutil.copy2(scenario_config_src, os.path.join(config_dir, "scenario.config"))

        # Copy generated configurations.yaml (not user-editable)
        configurations_src = os.path.join(out_template, "_config", "configurations.yaml")
        if os.path.exists(configurations_src):
            shutil.copy2(configurations_src, os.path.join(config_dir, "configurations.yaml"))

    # Return the file lists so the caller can add host mounts for user-editable files.
    # test_files are run-level (test_files_filter); config_files are config-specific.
    test_files = run_data.get("_test_files", [])
    config_files = [rel for rel, _ in cfg.get("_config_files", [])]

    execution_params = run_data.get("execution", {})
    secondary_containers = normalize_secondary_containers(
        execution_params.get("secondary_containers") or []
    )

    return vast_dir, test_files, config_files, secondary_containers


def generate_devcontainer_config(docker_image, uid, gid, gui=True,
                                 vast_dir=None, test_files=None, config_files=None,
                                 secondary_containers=None):
    """Generate VSCode devcontainer configuration files.

    Args:
        docker_image: Docker image to use (from project execution config)
        uid: User ID to run the container as
        gid: Group ID to run the container as
        gui: Whether to include X11/display mounts
        vast_dir: Absolute path to the directory containing the .vast file
                  (used to build host paths for test-file mounts)
        test_files: Run-level test files to mount directly from the host
        config_files: Config-level test files to mount directly from the host
        secondary_containers: List of normalized secondary container dicts
                              (each with at least a 'name' key)

    Returns:
        Tuple of (devcontainer_json_str, docker_compose_yml_str, restart_scripts)
        where restart_scripts is a dict mapping container name to shell script
        content for the corresponding /config/restart_<name>.sh helper.
    """
    test_files = test_files or []
    config_files = config_files or []
    secondary_containers = secondary_containers or []
    has_secondaries = bool(secondary_containers)

    # ------------------------------------------------------------------ main service
    compose_lines = [
        "services:",
        "  robovast:",
        f"    image: {docker_image}",
        "    command: sleep infinity",
    ]

    if has_secondaries:
        compose_lines.append("    ipc: shareable")

    compose_lines.append("    volumes:")
    compose_lines.append("      - ./config:/config:rw")

    # Mount test/config files from the host so edits in VSCode are live
    if vast_dir:
        for rel in test_files:
            host_path = os.path.join(vast_dir, rel).replace("\\", "/")
            compose_lines.append(f"      - {host_path}:/config/{rel}:rw")
        for rel in config_files:
            host_path = os.path.join(vast_dir, rel).replace("\\", "/")
            compose_lines.append(f"      - {host_path}:/config/{rel}:rw")

    if has_secondaries:
        compose_lines += [
            "      - shared_tmp:/tmp",
            "      - shared_ipc:/ipc",
            # Docker socket so restart scripts can restart secondary containers
            "      - /var/run/docker.sock:/var/run/docker.sock:ro",
        ]

    if gui:
        compose_lines += [
            "      - /tmp/.X11-unix:/tmp/.X11-unix:rw",
            "      - /dev/dri:/dev/dri",
        ]

    compose_lines.append("    environment:")
    if gui:
        compose_lines.append("      - DISPLAY=${DISPLAY:-:0}")
    compose_lines += [
        f"    user: \"{uid}:{gid}\"",
        "    tty: true",
        "    stdin_open: true",
    ]

    # ------------------------------------------------------------------ secondary services
    restart_scripts = {}
    for sc in secondary_containers:
        sc_name = sc["name"]
        container_name = f"{_SECONDARY_CONTAINER_PREFIX}{sc_name}"

        compose_lines += [
            f"  {sc_name}:",
            f"    image: {docker_image}",
            f"    container_name: {container_name}",
            "    network_mode: service:robovast",
            "    ipc: service:robovast",
            "    entrypoint: \"\"",
            "    depends_on:",
            "      - robovast",
            "    volumes:",
            "      - ./config:/config:ro",
        ]
        if vast_dir:
            for rel in test_files:
                host_path = os.path.join(vast_dir, rel).replace("\\", "/")
                compose_lines.append(f"      - {host_path}:/config/{rel}:ro")
            for rel in config_files:
                host_path = os.path.join(vast_dir, rel).replace("\\", "/")
                compose_lines.append(f"      - {host_path}:/config/{rel}:ro")
        compose_lines += [
            "      - shared_tmp:/tmp",
            "      - shared_ipc:/ipc",
        ]
        if gui:
            compose_lines += [
                "      - /tmp/.X11-unix:/tmp/.X11-unix:rw",
                "      - /dev/dri:/dev/dri",
            ]
        compose_lines.append("    environment:")
        compose_lines.append(f"      - CONTAINER_NAME={sc_name}")
        compose_lines.append("      - ROS_LOG_DIR=/out/logs")
        compose_lines.append("      - WATCHDOG_TIMEOUT=86400")
        compose_lines.append("      - CONNECT_TIMEOUT=86400")
        if gui:
            compose_lines.append("      - DISPLAY=${DISPLAY:-:0}")
        compose_lines += [
            f"    user: \"{uid}:{gid}\"",
            "    stop_signal: SIGINT",
            "    stop_grace_period: 5s",
            "    command: /bin/bash /config/secondary_entrypoint.sh",
            "    tty: true",
            "    stdin_open: true",
        ]

        restart_scripts[sc_name] = (
            "#!/bin/bash\n"
            f"docker restart {container_name}\n"
        )

    # ------------------------------------------------------------------ volumes block
    if has_secondaries:
        compose_lines += [
            "",
            "volumes:",
            "  shared_tmp:",
            "  shared_ipc:",
            "    driver: local",
            "    driver_opts:",
            "      type: tmpfs",
            "      device: tmpfs",
            '      o: "mode=0777"',
        ]

    docker_compose_yml = "\n".join(compose_lines) + "\n"

    # ------------------------------------------------------------------ devcontainer.json
    devcontainer = {
        "name": "robovast",
        "dockerComposeFile": "docker-compose.yml",
        "service": "robovast",
        "workspaceFolder": "/config",
        "postCreateCommand": (
            "echo 'source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null || true"
            " && source /ws/install/setup.bash 2>/dev/null || true' >> ~/.bashrc"
        ),
        "customizations": {
            "vscode": {
                "extensions": [
                    "ms-python.python",
                ]
            }
        },
    }
    devcontainer_json = json.dumps(devcontainer, indent=4) + "\n"

    return devcontainer_json, docker_compose_yml, restart_scripts
