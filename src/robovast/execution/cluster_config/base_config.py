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
        """Set up transfer mechanism for the cluster.

        Args:
            **kwargs: Cluster-specific configuration options
        """
        raise NotImplementedError("setup_cluster method must be implemented by subclasses.")

    def cleanup_cluster(self):
        """Clean up transfer mechanism for the cluster."""
        raise NotImplementedError("cleanup_cluster method must be implemented by subclasses.")

    def get_job_volumes(self):
        """Get volume definitions for job pods."""
        raise NotImplementedError("get_job_volumes method must be implemented by subclasses.")

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Prepare the cluster for the test run.

        Args:
            output_dir (str): Directory where setup files will be written
            **kwargs: Cluster-specific configuration options
        """
        raise NotImplementedError("prepare_setup_cluster method must be implemented by subclasses.")
