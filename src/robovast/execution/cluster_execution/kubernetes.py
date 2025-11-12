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

import subprocess
import sys

from kubernetes import client, config, utils
from kubernetes.client.rest import ApiException


def get_kubernetes_client():
    """Get a Kubernetes API client.
    """
    try:
        # Load kube config
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

        # Create API client
        return client.CoreV1Api()
    except Exception as e:
        print(f"### ERROR: Failed to create Kubernetes client: {str(e)}")
        return None


def check_pod_running(k8s_client, pod_name):
    """Check if transfer-pod exists, exit if not found"""
    try:
        pod = k8s_client.read_namespaced_pod(
            name=pod_name,
            namespace="default"
        )
        # Check if pod is running
        if pod.status.phase != "Running":
            return False, f"Pod is not running (status: {pod.status.phase})"
        return True, f"Pod '{pod_name}' is running"
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return False, f"Pod '{pod_name}' does not exist"
        else:
            return False, f"Error checking pod status: {str(e)}"


def apply_manifests(k8s_client, manifests: list):
    try:
        for yaml_object in manifests:
            if yaml_object is None:
                continue

            kind = yaml_object.get('kind')
            name = yaml_object.get('metadata', {}).get('name')

            try:
                # Use utils.create_from_dict to handle the resource creation
                utils.create_from_dict(k8s_client, yaml_object)
                print(f"Created {kind}/{name}")

            except ApiException as e:
                if e.status == 409:  # Already exists
                    print(f"{kind}/{name} already exists, skipping creation")
                raise
    except ApiException as e:
        raise RuntimeError(f"Failed to apply NFS manifest: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Error applying NFS manifest: {str(e)}") from e


def delete_manifests(core_v1, manifests: list):
    for yaml_object in manifests:
        if yaml_object is None:
            continue

        kind = yaml_object.get('kind')
        name = yaml_object.get('metadata', {}).get('name')
        namespace = yaml_object.get('metadata', {}).get('namespace', 'default')

        try:
            if kind == 'Pod':
                core_v1.delete_namespaced_pod(
                    name=name,
                    namespace=namespace,
                    body=client.V1DeleteOptions()
                )
                print(f"Deleted Pod/{name} from namespace {namespace}")

            elif kind == 'Service':
                core_v1.delete_namespaced_service(
                    name=name,
                    namespace=namespace,
                    body=client.V1DeleteOptions()
                )
                print(f"Deleted Service/{name} from namespace {namespace}")
            elif kind == 'ConfigMap':
                core_v1.delete_namespaced_config_map(
                    name=name,
                    namespace=namespace,
                    body=client.V1DeleteOptions()
                )
                print(f"Deleted ConfigMap/{name} from namespace {namespace}")
            elif kind == 'PersistentVolumeClaim':
                core_v1.delete_namespaced_persistent_volume_claim(
                    name=name,
                    namespace=namespace,
                    body=client.V1DeleteOptions()
                )
                print(f"Deleted PersistentVolumeClaim/{name} from namespace {namespace}")
            elif kind == 'PersistentVolume':
                core_v1.delete_persistent_volume(
                    name=name,
                    body=client.V1DeleteOptions()
                )
                print(f"Deleted PersistentVolume/{name}")
            elif kind == 'StorageClass':
                storage_api = client.StorageV1Api()
                storage_api.delete_storage_class(
                    name=name,
                    body=client.V1DeleteOptions()
                )
                print(f"Deleted StorageClass/{name}")
            else:
                raise RuntimeError(f"Unsupported kind for deletion: {kind}")

        except ApiException as e:
            if e.status == 404:  # Not found
                print(f"{kind}/{name} not found, skipping deletion")
            else:
                raise


def copy_config_to_cluster(config_dir, run_id):

    # Use kubectl cp to copy the entire config directory to the transfer pod
    try:
        print(f"### Copying config files to transfer pod using kubectl cp...")

        # Copy the config directory to the transfer pod at /exports/config/
        cmd = [
            "kubectl", "cp",
            config_dir,
            f"default/robovast:/exports/"
        ]

        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"### Successfully copied config files to transfer pod")

        # Verify the copy was successful by listing the directory
        verify_cmd = [
            "kubectl", "exec", "-n", "default", "robovast",
            "--",
            "ls", "-la", f"/exports/config/{run_id}"
        ]

        verify_result = subprocess.run(verify_cmd, capture_output=True, text=True, check=False)
        if verify_result.returncode == 0:
            print(f"### Verification: Config files successfully uploaded to /exports/config/{run_id}/")
        else:
            print(f"### Warning: Could not verify config file upload: {verify_result.stderr}")

    except subprocess.CalledProcessError as e:
        print(f"### ERROR: Failed to copy config files to transfer pod: {e}")
        print(f"### stdout: {e.stdout}")
        print(f"### stderr: {e.stderr}")
        sys.exit(1)
    except Exception as e:
        print(f"### ERROR: Unexpected error during config file copy: {e}")
        sys.exit(1)

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
