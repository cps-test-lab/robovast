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

"""Fill in what an already-recorded campaign's provenance can still be derived from.

Campaigns that ran before a field existed do not have it, and there is no way back to the
moment it was knowable. But some of it is still *derivable* -- a short sha can be resolved to a
full one while the commit is reachable, and a tag can be resolved to a digest while the
registry still serves it. Both get less recoverable over time, which is the argument for doing
it rather than waiting.

Three rules, and they are the whole design:

**Additive only.** Never overwrite a value the campaign recorded. A campaign's own record is
evidence; replacing part of it with something inferred later makes the rest unciteable, because
a reader can no longer tell which is which. Every key this writes is either absent or a
``backfilled_*`` sibling.

**Never invent.** A fact that cannot be derived is recorded as unknown *with its reason*, not
omitted and not guessed. "Nobody could tell" and "nobody looked" are different answers, and
only one of them means someone should look again.

**Idempotent, and dry by default.** It runs over published data; the second run must change
nothing, and the first must be inspectable before it happens.
"""

import logging
import os
import re
import subprocess  # nosec B404 - git/docker on operator-supplied paths
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

#: Where the derived values land. A separate block, not merged into the recorded fields, so the
#: campaign's own record stays exactly what it was and a reader can always tell the two apart.
BACKFILL_KEY = "backfilled"

#: Bumped when what this derives changes, so a re-run knows whether an existing block is stale.
#: Without it, "already backfilled" would mean "backfilled by some older version, contents
#: unknown" -- and the whole point is that a reader can tell what a value is.
BACKFILL_VERSION = 1

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHORT_SHA = re.compile(r"^[0-9a-f]{7,39}$")
_PROBE_TIMEOUT = 20


def _git(args, cwd) -> "str | None":
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                                check=False, timeout=_PROBE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _unknown(reason: str) -> dict:
    return {"value": None, "unknown": reason}


def _derive_full_revision(recorded: str) -> dict:
    """Resolve a recorded ``robovast_version`` to a full sha, when it is one and still exists.

    ``robovast_version`` is not reliably a revision at all: it falls back to the installed
    package semver when the git lookup failed, so ``2.0.0`` and ``abc1234`` both appear in that
    field. Only something sha-shaped is worth resolving, and only against this checkout -- a
    campaign recorded elsewhere may name a commit this clone has never had.
    """
    if not recorded:
        return _unknown("the campaign recorded no robovast_version")
    value = str(recorded).split("+", 1)[0].strip()
    if _FULL_SHA.match(value):
        return {"value": value, "source": "already a full sha"}
    if not _SHORT_SHA.match(value):
        return _unknown(f"robovast_version {recorded!r} is not a git revision (it is a package "
                        f"version, so no commit can be derived from it)")
    module_dir = os.path.dirname(os.path.abspath(__file__))
    resolved = _git(["rev-parse", f"{value}^{{commit}}"], module_dir)
    if not resolved or not _FULL_SHA.match(resolved):
        return _unknown(f"short revision {value!r} is not reachable from this checkout")
    return {"value": resolved, "source": f"resolved from the recorded short sha {value}"}


def _derive_image_digests(execution: dict) -> dict:
    """Classify each recorded image ref: already a digest, a local-only id, or a bare tag.

    A local id is the important case. ``docker inspect .Id`` was what the local lane recorded,
    and it cannot be pulled anywhere -- compose parses ``sha256:<hex>`` as ``name:tag`` and goes
    looking for ``docker.io/library/sha256``. Marking it as local-only is the honest answer and
    matches what ``campaign_pinned_images`` already concludes; silently promoting it to a digest
    would produce a re-run that fails at pull time for reasons nothing explains.
    """
    revisions = execution.get("image_revisions") or {}
    images = execution.get("images") or {}
    roles = sorted(set(revisions) | set(images))
    if not roles:
        return _unknown("the campaign recorded no images")

    out = {}
    for role in roles:
        recorded = str(revisions.get(role) or "")
        if "@sha256:" in recorded:
            out[role] = {"value": recorded, "source": "already a registry digest"}
        elif recorded.startswith("sha256:"):
            out[role] = _unknown(
                f"recorded as a local image id ({recorded[:19]}...), which cannot be pulled "
                f"anywhere; only the machine that ran it ever had these bytes")
        elif recorded:
            out[role] = _unknown(f"recorded as {recorded!r}, which is not a digest")
        else:
            out[role] = _unknown(f"no revision recorded for {role!r}")
    return {"per_role": out}


def plan_backfill(campaign_dir) -> dict:
    """What could be added to *campaign_dir*'s records, without writing anything.

    Returns ``{campaign_id, derived, already_present}``. Separate from :func:`apply_backfill` so
    the change is inspectable before it touches published data -- and so a caller can report
    across many campaigns without modifying any.
    """
    from robovast.common.campaign_data import read_execution_metadata

    campaign_dir = Path(campaign_dir)
    try:
        execution = read_execution_metadata(campaign_dir)
    except FileNotFoundError:
        return {"campaign_id": campaign_dir.name, "derived": {},
                "already_present": False,
                "unavailable": "no _execution/execution.yaml, so there is nothing to enrich"}

    existing = execution.get(BACKFILL_KEY) or {}
    derived = {
        "backfill_version": BACKFILL_VERSION,
        "robovast_revision": _derive_full_revision(execution.get("robovast_version")),
        "images": _derive_image_digests(execution),
    }
    return {"campaign_id": campaign_dir.name, "derived": derived,
            "already_present": existing.get("backfill_version") == BACKFILL_VERSION}


def apply_backfill(campaign_dir, *, force: bool = False) -> dict:
    """Write the derived block into ``_execution/execution.yaml``. Returns the plan.

    Rewrites the file rather than appending, because it is YAML -- but only ever *adds* the
    :data:`BACKFILL_KEY` block, so every value the campaign recorded survives byte-for-byte in
    meaning. ``force`` re-derives an existing block, which is what a bumped
    :data:`BACKFILL_VERSION` needs.
    """
    from robovast.common.campaign_data import read_execution_metadata

    plan = plan_backfill(campaign_dir)
    if plan.get("unavailable") or (plan["already_present"] and not force):
        return plan

    path = Path(campaign_dir) / "_execution" / "execution.yaml"
    execution = read_execution_metadata(Path(campaign_dir))
    execution[BACKFILL_KEY] = plan["derived"]
    # Written next to the original and moved into place: this is published data, and a crash
    # midway through a rewrite would leave a campaign with no execution record at all.
    tmp = path.with_suffix(".yaml.backfill-tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.dump(execution, handle, default_flow_style=False, sort_keys=False)
    os.replace(tmp, path)
    plan["written"] = True
    return plan
