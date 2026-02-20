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

import logging
import os
import sys

from kubernetes import client, config, utils
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


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
        logger.error(f"Failed to create Kubernetes client: {str(e)}")
        return None


def check_pod_running(k8s_client, pod_name, namespace="default"):
    """Check if transfer-pod exists, exit if not found"""
    try:
        pod = k8s_client.read_namespaced_pod(
            name=pod_name,
            namespace=namespace
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


def apply_manifests(k8s_client, manifests: list, namespace=None):
    """Apply Kubernetes manifests. If namespace is given, set it on each resource."""
    try:
        for yaml_object in manifests:
            if yaml_object is None:
                continue

            if namespace is not None:
                if 'metadata' not in yaml_object:
                    yaml_object['metadata'] = {}
                yaml_object['metadata']['namespace'] = namespace

            kind = yaml_object.get('kind')
            name = yaml_object.get('metadata', {}).get('name')

            try:
                # Use utils.create_from_dict to handle the resource creation
                utils.create_from_dict(k8s_client, yaml_object)
                logger.debug(f"Created {kind}/{name}")

            except ApiException as e:
                if e.status == 409:  # Already exists
                    logger.info(f"{kind}/{name} already exists, skipping creation")
                raise
    except ApiException as e:
        raise RuntimeError(f"Failed to apply manifest: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Error applying manifest: {str(e)}") from e


def delete_manifests(core_v1, manifests: list, namespace=None):
    """Delete Kubernetes resources from manifests. If namespace is given, use it for each resource."""
    for yaml_object in manifests:
        if yaml_object is None:
            continue

        if namespace is not None:
            if 'metadata' not in yaml_object:
                yaml_object['metadata'] = {}
            yaml_object['metadata']['namespace'] = namespace

        kind = yaml_object.get('kind')
        name = yaml_object.get('metadata', {}).get('name')
        ns = yaml_object.get('metadata', {}).get('namespace', 'default')

        try:
            if kind == 'Pod':
                core_v1.delete_namespaced_pod(
                    name=name,
                    namespace=ns,
                    body=client.V1DeleteOptions()
                )
                logger.debug(f"Deleted Pod/{name} from namespace {ns}")

            elif kind == 'Service':
                core_v1.delete_namespaced_service(
                    name=name,
                    namespace=ns,
                    body=client.V1DeleteOptions()
                )
                logger.debug(f"Deleted Service/{name} from namespace {ns}")
            elif kind == 'ConfigMap':
                core_v1.delete_namespaced_config_map(
                    name=name,
                    namespace=ns,
                    body=client.V1DeleteOptions()
                )
                logger.debug(f"Deleted ConfigMap/{name} from namespace {ns}")
            elif kind == 'PersistentVolumeClaim':
                core_v1.delete_namespaced_persistent_volume_claim(
                    name=name,
                    namespace=ns,
                    body=client.V1DeleteOptions()
                )
                logger.debug(f"Deleted PersistentVolumeClaim/{name} from namespace {ns}")
            elif kind == 'PersistentVolume':
                core_v1.delete_persistent_volume(
                    name=name,
                    body=client.V1DeleteOptions()
                )
                logger.debug(f"Deleted PersistentVolume/{name}")
            elif kind == 'StorageClass':
                storage_api = client.StorageV1Api()
                storage_api.delete_storage_class(
                    name=name,
                    body=client.V1DeleteOptions()
                )
                logger.debug(f"Deleted StorageClass/{name}")
            else:
                raise RuntimeError(f"Unsupported kind for deletion: {kind}")

        except ApiException as e:
            if e.status == 404:  # Not found
                logger.info(f"{kind}/{name} not found, skipping deletion")
            else:
                raise


def upload_configs_to_s3(config_dir, bucket_name, cluster_config, namespace="default"):
    """Upload run configuration files to S3 bucket.

    Creates the bucket and uploads the entire config_dir to the bucket root,
    preserving the directory structure.

    Args:
        config_dir (str): Local directory containing generated config files.
        bucket_name (str): S3 bucket name (e.g. 'run-20260220-123456').
        cluster_config: BaseConfig instance providing S3 endpoint/credentials.
        namespace (str): Kubernetes namespace (used for port-forwarding).
    """
    from .s3_client import ClusterS3Client

    if not os.path.isdir(config_dir):
        raise FileNotFoundError(f"Config directory does not exist: {config_dir}")

    access_key, secret_key = cluster_config.get_s3_credentials()

    logger.debug(f"Uploading config files to s3://{bucket_name}/ ...")
    try:
        with ClusterS3Client(
            namespace=namespace,
            access_key=access_key,
            secret_key=secret_key,
        ) as s3:
            s3.create_bucket(bucket_name)
            s3.upload_directory(bucket_name, config_dir)
        logger.debug(f"Successfully uploaded all config files to s3://{bucket_name}/")
    except Exception as e:
        logger.error(f"Failed to upload config files to S3: {e}")
        sys.exit(1)


def check_kubernetes_access(k8s_client, namespace="default"):
    """Check if Kubernetes cluster is accessible.

    Uses a namespace-scoped call (list pods in namespace) so that users with
    access only to a single namespace (e.g. RBAC) can pass cluster check.

    Args:
        k8s_client: CoreV1Api instance
        namespace: Namespace to check (avoids cluster-scoped list_namespace which
                   can return 403 for namespace-scoped users).

    Returns:
        tuple: (bool, str) - (success, message)
            - success: True if Kubernetes cluster is accessible, False otherwise
            - message: Success message or error description
    """
    try:
        # Try to get server version as a connectivity test
        version = client.VersionApi().get_code()
        k8s_version = f"{version.major}.{version.minor}"

        # Use namespace-scoped list so RBAC users with access only to one namespace succeed
        k8s_client.list_namespaced_pod(namespace=namespace, limit=1)

        return True, f"Kubernetes cluster is accessible (version {k8s_version}, namespace {namespace})"

    except config.ConfigException as e:
        return False, f"Kubernetes configuration not found: {str(e)}"

    except ApiException as e:
        return False, f"Kubernetes API error: {e.status} - {e.reason}"

    except Exception as e:
        return False, f"Failed to check Kubernetes access: {str(e)}"
