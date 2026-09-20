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
"""RKE2: bare-metal Kubernetes that provisions no volumes of its own.

Every directory this deployment keeps is a ``hostPath`` on the data node unless a class is
passed, which is why the defaults here are node directories. Campaigns live on the
service's results volume beside the workspaces; the ``robovast`` pod holds the registry and
the campaign index and is pinned to the same node.
"""
import logging

from .base_config import BaseConfig

logger = logging.getLogger(__name__)


class Rke2ClusterConfig(BaseConfig):

    def prepare_setup_cluster(self, output_dir, **kwargs):
        """Write the ``robovast`` pod manifest and the README for a manual setup."""
        self.write_store_pod_manifest(output_dir, **kwargs)
        readme_content = """# RKE2 Cluster Setup Instructions

Stock RKE2 provisions no volumes, so this deployment keeps its data in directories on
one node -- the data node -- and needs no storage class and no preparation on the nodes.

Finished campaigns live on the service's **results volume**, a directory beside the
workspaces on the data node (`--data-root` places both; `--workspaces-path` places the
workspaces and the results follow). Downloads, re-postprocessing and the campaign index
all read from there. It survives the service pod being restarted or upgraded, and
`vast cluster cleanup` leaves it alone -- `vast cluster cleanup --delete-data` is what
empties it. It is not a backup: one directory on one node is one disk, so archive anything
that must outlive the machine with `vast share`.

The `robovast` pod in this manifest holds the container registry and the campaign index.
Both are re-derivable -- images are rebuilt on demand and the index is re-ingested from
the campaigns -- and both are directories on the data node too (`--registry-path`,
and the index beside the results).

The directories draw from the node filesystem and declare no bound, so watch the web UI's
**Disk** meter: a hostPath carries no per-volume stats of its own, and the disk it shares
is the one that fills.

## Setup Steps

### 1. Apply the RoboVAST manifest

```bash
kubectl apply -f robovast-manifest.yaml
```

### 2. Wait for the pod to be ready

```bash
kubectl wait --for=condition=ready pod/robovast -n default --timeout=60s
```

The registry answers on `/v2` of the service's published host; the index on port 5432 of
the `robovast` Service, from inside the cluster only.
"""
        with open(f"{output_dir}/README_rke2.md", "w") as f:
            f.write(readme_content)

    def get_instance_type_command(self):
        """Get command to retrieve instance type of the current node."""
        return "INSTANCE_TYPE=$(uname -m)"
