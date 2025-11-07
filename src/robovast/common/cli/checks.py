#!/usr/bin/env python3
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

"""Docker and Kubernetes utilities for RoboVAST CLI."""

import docker
from docker.errors import DockerException
from kubernetes import client, config
from kubernetes.client.rest import ApiException


def check_docker_access():
    """Check if Docker is accessible and running.

    Returns:
        tuple: (bool, str) - (success, message)
            - success: True if Docker is accessible, False otherwise
            - message: Success message or error description
    """
    try:
        k8s_client = docker.from_env()
        # Try to ping the Docker daemon
        k8s_client.ping()

        # Get Docker version info for additional verification
        version_info = k8s_client.version()
        docker_version = version_info.get('Version', 'unknown')

        return True, f"Docker is accessible (version {docker_version})"

    except DockerException as e:
        return False, f"Docker daemon is not accessible: {str(e)}"

    except Exception as e:
        return False, f"Failed to check Docker access: {str(e)}"


def check_kubernetes_access(k8s_client):
    """Check if Kubernetes cluster is accessible.

    Returns:
        tuple: (bool, str) - (success, message)
            - success: True if Kubernetes cluster is accessible, False otherwise
            - message: Success message or error description
    """
    try:
        # Try to get server version as a connectivity test
        version = client.VersionApi().get_code()
        k8s_version = f"{version.major}.{version.minor}"

        # Try to list namespaces to verify we have basic permissions
        k8s_client.list_namespace(limit=1)

        return True, f"Kubernetes cluster is accessible (version {k8s_version})"

    except config.ConfigException as e:
        return False, f"Kubernetes configuration not found: {str(e)}"

    except ApiException as e:
        return False, f"Kubernetes API error: {e.status} - {e.reason}"

    except Exception as e:
        return False, f"Failed to check Kubernetes access: {str(e)}"
