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

from robovast.common.campaign_data import image_is_pullable

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

    from robovast.common.campaign_data import campaign_image_record  # noqa: PLC0415

    return [*_classify_robovast(execution),
            *_classify_images(campaign_image_record(campaign_dir)),
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


def _classify_images(record) -> list:
    """Each container image: could somebody else obtain these bytes, or rebuild them?

    One policy over :func:`~robovast.common.campaign_data.campaign_image_record`, which is what
    keeps this and the retrigger pre-flight from disagreeing about one file on one disk.

    Two ways to be reproducible:

    * **a digest** identifies bytes exactly. Whether they are *fetchable* is an access question,
      which is what ``private`` is for.
    * **a recipe** -- the base it was built FROM and the dated archives it installed from --
      rebuilds them. It outlives the digest: a registry keeps a manifest for as long as it keeps
      it, and the recipe is what still answers afterwards. A campaign carrying a complete
      recipe and its build lock is reproducible even when its digest is gone.
    """
    roles = sorted(record.roles)
    if not roles:
        return [_entry("images", OPAQUE, "no container image recorded")]

    out = []
    for name in roles:
        role = record.roles[name]
        label = f"image[{name}]"
        # Either field may hold the digest. The two lanes fill them differently -- the local one
        # records a plan-resolved ref under `images` and a local id under `image_revisions`, the
        # cluster one the reverse -- so reading only one reported a campaign whose other field
        # was already `repo@sha256:...` as having no digest at all, while quoting that very
        # digest back in the message.
        digest = next((ref for ref in (role.recorded, role.declared)
                       if image_is_pullable(ref)), "")
        if digest:
            kind = PUBLIC if _looks_public(digest) or digest.startswith("ghcr.io") else PRIVATE
            out.append(_entry(label, kind,
                              "pinned by registry digest"
                              + ("" if kind == PUBLIC else
                                 " in a registry that needs access; the digest identifies the "
                                 "exact bytes, so anyone with access can reproduce it"),
                              digest=digest))
            continue

        rebuildable = _recipe_entry(label, role)
        if rebuildable is not None:
            out.append(rebuildable)
        elif role.recorded.startswith("sha256:"):
            out.append(_entry(label, OPAQUE,
                              "recorded only as a LOCAL image id, which cannot be pulled "
                              "anywhere -- only the machine that ran it ever had these bytes",
                              recorded=role.recorded))
        else:
            shown = role.declared or role.recorded or "nothing"
            out.append(_entry(label, OPAQUE,
                              f"no digest recorded; {shown!r} is a mutable reference and will "
                              f"not name the same bytes later",
                              recorded=role.recorded or role.declared))
    return out


#: What a recipe has to name before a rebuild would reproduce rather than approximate: where it
#: started, and the dated archives that keep `apt-get install` resolving the same versions.
#: Two of the three is not a partial answer -- it is a rebuild that installs whatever is current.
_RECIPE_KEYS = ("base_image", "ubuntu_snapshot", "ros_snapshot")


def _recipe_entry(label: str, role):
    """The role classified by its build recipe, or ``None`` when it has no usable one."""
    missing = [key for key in _RECIPE_KEYS if not role.build_refs.get(key)]
    if missing:
        return None
    if not role.has_lock:
        # The recipe says where to start; the lock says which versions. Without it a rebuild
        # re-resolves the author's loose specs and gets whatever is current -- a different
        # experiment wearing the same name -- so this is not yet an identity.
        return _entry(label, OPAQUE,
                      "no digest, and its build recipe has no build lock beside it, so a "
                      "rebuild would re-resolve package versions rather than reproduce them",
                      recipe={key: role.build_refs.get(key) for key in _RECIPE_KEYS})
    source = str(role.build_refs.get("source") or "")
    kind = PUBLIC if _looks_public(source) else PRIVATE
    return _entry(label, kind,
                  "no digest, but recorded a complete build recipe and lock -- the base it was "
                  "built from, the dated archives it installed from, and the resolved versions "
                  "-- so it can be rebuilt"
                  + ("" if kind == PUBLIC else
                     ", by anyone who can reach the sources it names"),
                  recipe={key: role.build_refs.get(key) for key in _RECIPE_KEYS})


def _classify_plugins(record) -> list:
    """Third-party plugins: a version or a commit is identity; neither is opaque."""
    if record is None:
        # Unknown, not empty: a campaign from before this was captured may well have declared
        # plugins. Treated as opaque because the dataset cannot show otherwise.
        return [_entry("plugins", OPAQUE,
                       "no plugin resolution recorded, so if this campaign declared any their "
                       "versions are unknown and a re-run would resolve them afresh")]
    if not record:
        # Recorded, and there were none. Nothing to identify, so this contributes no input at all
        # rather than a "public" one -- counting an absence as an identified input would flatter
        # the manifest. Distinguishable from the branch above only because the writer now records
        # an empty set; without that, every campaign with no plugins would be refused here.
        return []
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
    if not record:
        # Recorded, and there were none -- a campaign with no simulator has no asset providers.
        # Contributes no input, for the reason _classify_plugins gives.
        return []
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
