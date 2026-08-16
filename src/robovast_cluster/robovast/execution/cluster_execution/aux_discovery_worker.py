# Copyright (C) 2026 Frederik Pasch
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

"""Isolated aux-container discovery worker.

Runs :func:`container_runner._discover_specs` in a **fresh subprocess** so that a
``.vast``'s declared variation-plugin packages (and their pinned dependencies —
which may conflict with robovast's own, e.g. a forked ``rdflib``) are imported
*here*, never in the long-lived robovast service. The project's
``.robovast_plugins/`` leads ``sys.path`` in this process (installed and prepended
by ``ensure_workspace_plugins``), so the plugin's ``robovast.variation_types`` entry
points resolve and ``get_required_container`` can be called on each variation.

Invoked by ``container_runner._discover_specs_subprocess`` as::

    python -m robovast.execution.cluster_execution.aux_discovery_worker <job.json>

where ``job.json`` is ``{config_path, result_path}``. The discovered
``ContainerSpec`` list is written to ``result_path`` as JSON (one ``asdict`` per
spec), which the parent rebuilds into ``ContainerSpec`` objects. Any plugin
traceback goes to stdout/stderr, which the parent captures for its warning.
"""

import dataclasses
import json
import sys


def main(argv) -> int:
    if len(argv) != 2:
        print("usage: python -m "
              "robovast.execution.cluster_execution.aux_discovery_worker <job.json>",
              file=sys.stderr)
        return 2

    with open(argv[1], encoding="utf-8") as f:
        job = json.load(f)

    # Imported lazily; this is also where the plugin gets pulled onto sys.path
    # (inside _discover_specs via ensure_workspace_plugins).
    from .container_runner import (  # pylint: disable=import-outside-toplevel
        _discover_specs)

    specs = _discover_specs(job["config_path"])

    with open(job["result_path"], "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(spec) for spec in specs], f)
    return 0


if __name__ == "__main__":
    # Let any exception propagate: Python prints the full traceback to stderr (which
    # the parent captures) and exits non-zero, so the parent raises AuxDiscoveryError
    # with that traceback rather than proceeding with no aux pod.
    sys.exit(main(sys.argv))
