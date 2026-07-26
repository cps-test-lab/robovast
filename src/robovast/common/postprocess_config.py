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

"""Resolve the ``.vast`` a campaign's postprocessing should use.

A campaign keeps the immutable ``.vast`` it ran with under ``_config/`` (never
mutated) and any *edited* postprocessing configs as versioned overrides under
``_control/postprocess/rev-N.vast`` (see ``service.postprocessing_edit``). The
effective config is the latest override, else the snapshot.

This lives in ``common`` — with no dependency on ``service`` or ``execution`` — so
every layer resolves it the same way: the service edit/re-run path, the CLI, and
the in-cluster conversion Job (``execution.cluster_execution.postprocess_job``).
Putting it here is what lets an edited ``rev-N.vast`` actually take effect on the
cluster path, not only locally.
"""

import re
from pathlib import Path

#: Where postprocessing overrides live under a campaign dir.
POSTPROCESS_SUBDIR = "_control/postprocess"
_REV_RE = re.compile(r"^rev-(\d+)\.vast$")


def config_vast(campaign_dir: Path) -> Path:
    """The immutable snapshot ``.vast`` in ``_config/`` (never modified)."""
    config_dir = Path(campaign_dir) / "_config"
    vasts = sorted(config_dir.glob("*.vast"))
    if not vasts:
        raise ValueError(f"no .vast snapshot in {config_dir}")
    return vasts[0]


def rev_dir(campaign_dir: Path) -> Path:
    return Path(campaign_dir) / POSTPROCESS_SUBDIR


def revs(campaign_dir: Path) -> list[tuple[int, Path]]:
    """All override revisions as ``(n, path)``, ascending."""
    d = rev_dir(campaign_dir)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        m = _REV_RE.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def effective_vast(campaign_dir: Path) -> Path:
    """The `.vast` postprocessing currently uses: latest override, else snapshot."""
    campaign_dir = Path(campaign_dir)
    r = revs(campaign_dir)
    return r[-1][1] if r else config_vast(campaign_dir)
