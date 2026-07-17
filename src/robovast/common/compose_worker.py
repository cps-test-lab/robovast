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

"""Isolated scenario-composition worker.

Runs :func:`robovast.common.config_generation.generate_scenario_variations` in a
**fresh subprocess** so that a ``.vast``'s declared variation-plugin packages (and
their pinned dependencies — which may conflict with robovast's own, e.g. a forked
``rdflib``) are imported *here*, never in the long-lived robovast process. The
project's ``.robovast_plugins/`` leads ``sys.path`` in this process (installed and
prepended by ``ensure_workspace_plugins`` during composition), so the plugin's
versions win locally and cannot clash with the parent.

Invoked by ``config_generation._compose_isolated`` as::

    python -m robovast.common.compose_worker <job.json>

where ``job.json`` is ``{variation_file, output_dir, use_cache, result_path}``. The
parent sets ``ROBOVAST_ISOLATED_COMPOSE=1`` in this process's environment so the
``generate_scenario_variations`` call composes in-process (it does not re-fork).

Progress (including ``pip install`` output for a first-time plugin install) and any
plugin traceback go to stdout/stderr, which the parent streams live and captures for
its error message. The composed ``campaign_data`` is written to ``result_path`` using
the shared cache transport, so the parent reconstructs it identically to a cache hit.
Artifacts are written straight into the shared ``output_dir`` on disk.
"""

import json
import sys


def main(argv) -> int:
    if len(argv) != 2:
        print("usage: python -m robovast.common.compose_worker <job.json>",
              file=sys.stderr)
        return 2

    with open(argv[1], encoding="utf-8") as f:
        job = json.load(f)

    # Imported lazily so the module is cheap to import for tooling; this is also
    # where the plugin gets pulled onto sys.path (inside generate_scenario_variations
    # via ensure_workspace_plugins).
    from robovast.common.config_generation import (  # pylint: disable=import-outside-toplevel
        _result_to_transport, generate_scenario_variations)

    result, _gui_classes = generate_scenario_variations(
        variation_file=job["variation_file"],
        output_dir=job["output_dir"],
        use_cache=job.get("use_cache", True),
        # Stream progress to stdout so the parent can forward it live.
        progress_update_callback=lambda m: print(m, flush=True),
    )

    with open(job["result_path"], "w", encoding="utf-8") as f:
        json.dump(_result_to_transport(result), f)
    return 0


if __name__ == "__main__":
    # Let any composition exception propagate: Python prints the full traceback to
    # stderr (which the parent captures) and exits non-zero, so the parent raises a
    # RuntimeError carrying the real plugin error.
    sys.exit(main(sys.argv))
