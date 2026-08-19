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

"""Can somebody else reproduce this campaign, and if not, exactly which input stops them.

Publication is the last moment the answer is cheap. Afterwards the dataset has a DOI, is cited,
and every gap in it is permanent -- so the check belongs here rather than anywhere upstream: it
is where the dataset stops being ours to fix.

Three classes, and the middle one is the point:

``public``
    a public repo at a pinned commit, a registry digest, a pinned package. Anyone can obtain it.
``private``
    reachable only with access -- but its **commit is recorded**, so somebody who has that access
    can reproduce it exactly. Allowed, and named in the dataset, because a private input is not
    the same failure as an unidentifiable one.
``opaque``
    nothing recorded that could identify it. A floating ref, a dirty tree, a local-only image id,
    an image with no provenance. **This is what blocks publication**: the dataset would depend on
    something nobody -- including us -- can name a year from now.

The distinction between ``private`` and ``opaque`` is the whole design. Refusing every input that
a stranger cannot fetch would refuse most real research; refusing only the ones nobody can
*identify* is a claim that can actually be honoured.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PUBLIC = "public"
PRIVATE = "private"
OPAQUE = "opaque"

#: Hosts whose content anyone can fetch. Deliberately a short list of forges rather than a
#: guess: "looks like a URL" is not evidence of being obtainable, and a wrong `public` here is
#: worse than an unnecessary `private` -- one overstates the dataset, the other understates it.
_PUBLIC_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "sr.ht",
                 "pypi.org", "files.pythonhosted.org")


def _entry(name: str, kind: str, why: str, **extra) -> dict:
    return {"input": name, "class": kind, "why": why, **extra}


def _looks_public(url: str) -> bool:
    return any(host in (url or "") for host in _PUBLIC_HOSTS)


def classify_campaign_inputs(campaign_dir) -> list:
    """One record per input this campaign depended on, classified.

    Reads only what the campaign recorded. That is the point: if a fact was not recorded it
    cannot be recovered now, and reporting it as opaque is the honest answer rather than going
    to look for it in the environment that happens to be running this.
    """
    from robovast.common.campaign_data import (read_execution_metadata, read_plugins_record,
                                               read_providers_record)

    campaign_dir = Path(campaign_dir)
    try:
        execution = read_execution_metadata(campaign_dir)
    except FileNotFoundError:
        return [_entry("_execution/execution.yaml", OPAQUE,
                       "the campaign recorded no execution metadata at all, so nothing about "
                       "what ran can be identified")]

    return [*_classify_robovast(execution),
            *_classify_images(execution),
            *_classify_plugins(read_plugins_record(campaign_dir)),
            *_classify_providers(read_providers_record(campaign_dir))]


def _classify_robovast(execution: dict) -> list:
    """robovast itself: a public repo, so what matters is whether a commit was recorded."""
    revision = execution.get("robovast_revision")
    if execution.get("robovast_dirty"):
        return [_entry("robovast", OPAQUE,
                       "the campaign ran from a DIRTY checkout, so the recorded revision does "
                       "not describe the code that produced these results and nobody -- "
                       "including us -- can reconstruct it",
                       revision=revision)]
    if not revision:
        return [_entry("robovast", OPAQUE,
                       f"no robovast_revision recorded; robovast_version "
                       f"{execution.get('robovast_version')!r} is a package version and cannot "
                       f"name a commit. Try 'vast results backfill-provenance' -- it can "
                       f"sometimes still derive one.")]
    return [_entry("robovast", PUBLIC, "public repository at a recorded commit",
                   revision=revision)]


def _classify_images(execution: dict) -> list:
    """Each container image: a digest is identity, a local id is not."""
    revisions = execution.get("image_revisions") or {}
    images = execution.get("images") or {}
    roles = sorted(set(revisions) | set(images))
    if not roles:
        return [_entry("images", OPAQUE, "no container image recorded")]

    out = []
    for role in roles:
        recorded = str(revisions.get(role) or "")
        declared = str(images.get(role) or "")
        name = f"image[{role}]"
        # Either field may hold the digest. The two lanes fill them differently -- the local one
        # records a plan-resolved ref under `images` and a local id under `image_revisions`, the
        # cluster one the reverse -- so reading only `image_revisions` reported a campaign whose
        # `images` entry was already `repo@sha256:...` as having no digest at all, while quoting
        # that very digest back in the message.
        if "@sha256:" not in recorded and "@sha256:" in declared:
            recorded = declared
        if "@sha256:" in recorded:
            # A digest identifies bytes. Whether they are *fetchable* depends on the registry,
            # which is an access question -- the same one `private` exists for.
            kind = PUBLIC if _looks_public(recorded) or recorded.startswith("ghcr.io") else PRIVATE
            out.append(_entry(name, kind,
                              "pinned by registry digest"
                              + ("" if kind == PUBLIC else
                                 " in a registry that needs access; the digest identifies the "
                                 "exact bytes, so anyone with access can reproduce it"),
                              digest=recorded))
        elif recorded.startswith("sha256:"):
            out.append(_entry(name, OPAQUE,
                              "recorded only as a LOCAL image id, which cannot be pulled "
                              "anywhere -- only the machine that ran it ever had these bytes",
                              recorded=recorded))
        else:
            out.append(_entry(name, OPAQUE,
                              f"no digest recorded; {declared or recorded or 'nothing'!r} is a "
                              f"mutable reference and will not name the same bytes later",
                              recorded=recorded or declared))
    return out


def _classify_plugins(record) -> list:
    """Third-party plugins: a version or a commit is identity; neither is opaque."""
    if record is None:
        # Unknown, not empty: a campaign from before this was captured may well have declared
        # plugins. Treated as opaque because the dataset cannot show otherwise.
        return [_entry("plugins", OPAQUE,
                       "no plugin resolution recorded, so if this campaign declared any their "
                       "versions are unknown and a re-run would resolve them afresh")]
    out = []
    for name, info in sorted(record.items()):
        commit, version, url = info.get("commit"), info.get("version"), info.get("url", "")
        if commit:
            kind = PUBLIC if _looks_public(url) else PRIVATE
            out.append(_entry(f"plugin[{name}]", kind, "pinned to a commit", commit=commit))
        elif version:
            out.append(_entry(f"plugin[{name}]", PUBLIC, "pinned to a released version",
                              version=version))
        else:
            out.append(_entry(f"plugin[{name}]", OPAQUE,
                              "declared but resolved nowhere this record can name, so the code "
                              "that ran came from an unidentifiable location"))
    return out


def _classify_providers(record) -> list:
    """Asset providers: usually private, and that is fine as long as they are named."""
    if record is None:
        return [_entry("providers", OPAQUE,
                       "no asset providers recorded, so which world and model packages supplied "
                       "this campaign cannot be identified")]
    out = []
    for name, info in sorted(record.items()):
        commit, url = info.get("commit"), info.get("url", "")
        kind = PUBLIC if _looks_public(url) else PRIVATE
        detail = ("pinned to a commit" if commit else
                  f"version {info.get('version')} recorded, but no commit -- reproducible only "
                  f"if that version is still obtainable")
        out.append(_entry(f"provider[{name}]", kind, detail,
                          version=info.get("version"), commit=commit))
    return out


def reproducibility_manifest(campaign_dir) -> dict:
    """``{campaign_id, publishable, opaque, counts, inputs}`` for *campaign_dir*."""
    inputs = classify_campaign_inputs(campaign_dir)
    opaque = [entry["input"] for entry in inputs if entry["class"] == OPAQUE]
    counts = {kind: sum(1 for e in inputs if e["class"] == kind)
              for kind in (PUBLIC, PRIVATE, OPAQUE)}
    return {"campaign_id": Path(campaign_dir).name,
            "publishable": not opaque,
            "opaque": opaque,
            "counts": counts,
            "inputs": inputs}
