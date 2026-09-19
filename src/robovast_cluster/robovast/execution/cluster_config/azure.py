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
"""AKS: a managed node pool, whose machines are replaced rather than repaired.

Nothing here is provider-specific beyond how a node reports its VM size. What matters on
AKS is how the volumes are backed: a node directory goes with the node, so the README
says to pass a StorageClass for each tenant (``managed-csi`` is the stock one).
"""
import logging

from .base_config import BaseConfig

logger = logging.getLogger(__name__)


class AzureClusterConfig(BaseConfig):

    #: AKS node pools are Azure VMs, whose guest kernels expose no cpufreq policy.
    #: See :attr:`BaseConfig.governor_is_settable`.
    governor_is_settable = False

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Write the ``robovast`` pod manifest and the README for a manual setup."""
        self.write_store_pod_manifest(output_dir, **kwargs)
        readme_content = """# Azure Cluster Setup Instructions

Finished campaigns live on the service's **results volume**, beside the workspaces; the
`robovast` pod in this manifest holds the container registry and the campaign index.

On AKS, back every volume with a StorageClass rather than a node directory: a managed
node pool replaces machines, and a hostPath goes with the machine. `managed-csi` is the
stock class:

```bash
vast cluster setup azure \\
    --workspaces-class managed-csi \\
    --index-class managed-csi \\
    --registry-class managed-csi \\
    --buildkit-class managed-csi
```

`--workspaces-class` backs the workspaces and the results with it, which is where the
campaigns are. Keeping those disks is the operator's snapshot schedule, which RoboVAST
does not manage.

## Setup Steps

### 1. Apply the RoboVAST manifest

```bash
kubectl apply -f robovast-manifest.yaml
```

### 2. Wait for the pod to be ready

```bash
kubectl wait --for=condition=ready pod/robovast --timeout=120s
```

The registry answers on `/v2` of the service's published host; the index on port 5432 of
the `robovast` Service, from inside the cluster only.
"""
        with open(f"{output_dir}/README_azure.md", "w") as f:
            f.write(readme_content)

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        return (
            'INSTANCE_TYPE=$(curl -s -H "Metadata: true" '
            '"http://169.254.169.254/metadata/instance/compute/vmSize'
            '?api-version=2021-02-01&format=text")'
        )
