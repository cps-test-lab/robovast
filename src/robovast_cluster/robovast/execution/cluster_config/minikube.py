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
"""Minikube: a one-node development cluster.

The same deployment as RKE2 -- node directories for everything, the ``robovast`` pod for
the registry and the index, campaigns on the service's results volume -- on a machine
that is usually the developer's own.
"""
import logging

from .base_config import BaseConfig

logger = logging.getLogger(__name__)


class MinikubeClusterConfig(BaseConfig):

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Write the ``robovast`` pod manifest and the README for a manual setup."""
        self.write_store_pod_manifest(output_dir, **kwargs)
        readme_content = """# Minikube Cluster Setup Instructions

Every directory this deployment keeps is a hostPath on the minikube node. Finished
campaigns live on the service's **results volume**, beside the workspaces; the
`robovast` pod in this manifest holds the container registry and the campaign index.
Suitable for development and short-lived runs: archive anything that must outlive the
machine with `vast share`, and empty the directories with
`vast cluster cleanup --delete-data`.

## Setup Steps

### 1. Apply the RoboVAST manifest

```bash
kubectl apply -f robovast-manifest.yaml
```

### 2. Wait for the pod to be ready

```bash
kubectl wait --for=condition=ready pod/robovast --timeout=60s
```

The registry answers on `/v2` of the service's published host; the index on port 5432 of
the `robovast` Service, from inside the cluster only.
"""
        with open(f"{output_dir}/README_minikube.md", "w") as f:
            f.write(readme_content)

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        return "INSTANCE_TYPE=$(uname -m)"
