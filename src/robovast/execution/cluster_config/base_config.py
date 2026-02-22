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


class BaseConfig(object):
    """Base class for cluster configurations."""

    def setup_cluster(self, **kwargs):
        """Set up the MinIO S3 server in the cluster.

        Args:
            **kwargs: Cluster-specific configuration options
        """
        raise NotImplementedError("setup_cluster method must be implemented by subclasses.")

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        raise NotImplementedError("get_instance_type_command method must be implemented by subclasses.")

    def cleanup_cluster(self, **kwargs):
        """Clean up the MinIO S3 server from the cluster.

        Args:
            **kwargs: Cluster-specific configuration options
        """
        raise NotImplementedError("cleanup_cluster method must be implemented by subclasses.")

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Prepare the cluster for the test run.

        Args:
            output_dir (str): Directory where setup files will be written
            **kwargs: Cluster-specific configuration options
        """
        raise NotImplementedError("prepare_setup_cluster method must be implemented by subclasses.")

    def get_s3_endpoint(self) -> str:
        """Return the cluster-internal S3 endpoint URL for the embedded MinIO server.

        Subclasses may override this to point to an external S3 service.

        Returns:
            str: S3 endpoint URL, e.g. 'http://robovast:9000'
        """
        return "http://robovast:9000"

    def get_s3_credentials(self) -> tuple:
        """Return the (access_key, secret_key) pair for the S3 server.

        Subclasses may override this to supply credentials for an external S3 service.

        Returns:
            tuple[str, str]: (access_key, secret_key)
        """
        return ("minioadmin", "minioadmin")
