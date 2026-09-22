#!/usr/bin/env python3
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

"""How an archive is named on a share, and how that name is read back.

A share holds two kinds of archive, and the name is the only thing that tells them
apart: ``<campaign-id>.<variant>.tar.gz`` for a campaign's results and
``<slug>.workspace.tar.gz`` for a workspace's project files. The two grammars cannot
collide -- a campaign id must match :func:`is_campaign_dir` once its token is taken
off, and a workspace slug is read only from a name carrying the ``.workspace`` token,
which that pattern never leaves behind -- so a listing can classify every object it
sees without opening any of them.

A campaign's name carries the *variant* because a share holds both and they are not
interchangeable: a ``raw`` archive is the campaign as it stood before
postprocessing, so importing one has metrics still to compute, while a
``postprocessed`` one is complete. Nothing else records this -- there is no
manifest beside the object and no database of what was uploaded -- so a name that
did not say it would leave the only answer "download it and look inside".

No dependency beyond :mod:`robovast.common.execution` on purpose: the providers,
the execution backends and the CLI all need these two functions, and a shared
helper that drags in ``click`` or a provider ABC would be imported by none of them
willingly.
"""

import re
from pathlib import Path

#: Postprocessing's provenance record, campaign-relative. Written by
#: ``results_processing.postprocessing`` and by nothing else, at the end of the
#: command run, listing one entry per derived output with its sources and plugin.
from robovast.common.campaign_data import POSTPROCESSING_RECORD

from robovast.common.execution import is_campaign_dir

__all__ = ["RAW", "POSTPROCESSED", "INCOMPLETE", "VARIANTS", "SHARE_VARIANTS",
           "POSTPROCESSING_RECORD", "WORKSPACE",
           "archive_name", "parse_archive_name", "campaign_variant", "variant_from_record",
           "workspace_slug", "workspace_archive_name", "parse_workspace_archive_name",
           "is_share_archive_name"]

RAW = "raw"
POSTPROCESSED = "postprocessed"
#: A campaign archived while it was still running. Not a postprocessing state like the
#: other two but a completeness one, and it sits in the same slot because it answers the
#: same question a variant answers -- "what will I get if I import this?" -- and because a
#: name is the only thing a file carries into a downloads directory. Runs that had not
#: finished are simply absent, so it is the one variant an importer must warn about.
INCOMPLETE = "incomplete"

#: Longest first, so ``.postprocessed`` is never read as an unsuffixed name.
VARIANTS = (POSTPROCESSED, INCOMPLETE, RAW)

#: The variants a *share* can hold. A share copy is written at the campaign's end, so
#: :data:`INCOMPLETE` is not among them -- it names a download taken mid-run, which nothing
#: uploads. Offered to a caller choosing a variant; :data:`VARIANTS` stays the set a name
#: is *read* against, because a name that arrives from elsewhere is not ours to bound.
SHARE_VARIANTS = (POSTPROCESSED, RAW)

#: The token naming a workspace archive, in the slot a campaign archive keeps its variant
#: in. A workspace has no variants -- a project tree is whatever it currently is -- so the
#: token is the *kind* instead, and it is what makes the two grammars mutually exclusive.
WORKSPACE = "workspace"

_SUFFIX = ".tar.gz"

#: What a workspace name may keep in an object name. Everything else becomes ``-``: a name
#: is free text a person typed, and it travels through provider keys, URLs and a
#: downloads directory, none of which agree about spaces, slashes or quoting.
_SLUG_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def archive_name(campaign_id: str, variant: str = RAW) -> str:
    """Return the object name a campaign's *variant* archive is stored under.

    ``<campaign-id>.<variant>.tar.gz`` -- the variant sits between the id and the
    extension rather than inside the id, so :func:`is_campaign_dir` still sees an
    untouched campaign id once the known token is taken off.
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown archive variant {variant!r}; expected one of "
                         f"{', '.join(VARIANTS)}")
    return f"{campaign_id}.{variant}{_SUFFIX}"


def parse_archive_name(basename: str):
    """Read ``(campaign_id, variant)`` out of *basename*, or ``None`` if it is not one.

    An archive written before the variant was part of the name has no token, and is
    read as :data:`RAW` -- which is what it is, since the only thing that ever wrote
    one was the campaign-end upload, and that runs before postprocessing.
    """
    if not basename.endswith(_SUFFIX):
        return None
    # A separator here means the caller passed a key, not a base name. Refused rather than
    # stripped: ``is_campaign_dir`` matches on ``.+-<timestamp>``, and ``.`` matches ``/``,
    # so ``results/nav-2026-08-18-194018`` parses as a perfectly good campaign id with a
    # path separator inside it -- which is then used as a directory name.
    if "/" in basename or "\\" in basename:
        return None
    stem = basename[: -len(_SUFFIX)]
    for variant in VARIANTS:
        token = f".{variant}"
        if stem.endswith(token):
            campaign_id = stem[: -len(token)]
            return (campaign_id, variant) if is_campaign_dir(campaign_id) else None
    return (stem, RAW) if is_campaign_dir(stem) else None


def variant_from_record(record) -> str:
    """Which variant a campaign is, given the bytes of its :data:`POSTPROCESSING_RECORD`.

    *record* is the file's contents, or ``None`` when the campaign has no such file.

    Why this file and not a derived artifact. The variant has to be decidable from the
    archive ALONE -- a recipient analyses it without our service and without the results
    index -- so querying the index is out, and after the per-campaign ``data.db`` was
    dropped in favour of that index there is no single derived file left to point at:
    what postprocessing leaves in the directory is per-run CSVs whose names come from the
    campaign's own plugin list, so "is there derived data?" would mean guessing at names
    a stranger's campaign chose. The provenance record is the one thing postprocessing
    always writes, under a fixed name, and it is *self-describing*: it does not merely
    imply that derived data exists, it says which files were derived from which sources
    by which plugin -- exactly what the recipient of a ``postprocessed`` archive needs.

    An empty ``entries`` list is :data:`RAW`, deliberately. The file is written even when
    every step failed or none was configured, and calling that ``postprocessed`` would
    hand a reader a campaign with no derived data under a name promising results -- the
    one direction of error that is not recoverable by looking.

    A record that cannot be parsed raises: it is postprocessing's own output in a format
    postprocessing wrote, so a broken one is a real defect in the campaign, and the two
    silent answers available here ("raw" or "postprocessed") would both be inventions.
    """
    from robovast.common.campaign_data import \
        postprocessing_entries  # pylint: disable=import-outside-toplevel

    entries = postprocessing_entries(record)
    return POSTPROCESSED if entries else RAW


def campaign_variant(campaign_root) -> str:
    """Which variant a campaign *directory* would be archived as.

    Reads the directory rather than being told by whoever happened to know, so that the
    campaign-end upload and a later ``vast share export`` cannot disagree. What is read,
    and why it is the right evidence offline, is in :func:`variant_from_record`.
    """
    path = Path(campaign_root) / POSTPROCESSING_RECORD
    try:
        record = path.read_bytes()
    except FileNotFoundError:
        record = None
    return variant_from_record(record)


def workspace_slug(name: str, fallback: str = "") -> str:
    """The share-safe slug a workspace called *name* is published under.

    A workspace name is free text, so it is reduced to ``[A-Za-z0-9._-]`` and falls back
    to *fallback* (the workspace id) when nothing survives. The slug is what identifies
    the archive on the share and what an import offers as the new workspace's name --
    there is no manifest inside the archive, because a workspace that arrives somewhere
    else is a project to be worked on, not a record to be preserved under its old
    identity.

    Two workspaces whose names slug the same publish to the same object, exactly as
    re-exporting one does: the share holds the latest upload under a name, and which
    workspace is *here* is the registry's answer, not the share's.
    """
    slug = _SLUG_SAFE.sub("-", name).strip("-")
    return slug or fallback


def workspace_archive_name(slug: str) -> str:
    """``<slug>.workspace.tar.gz`` -- the object name a workspace is stored under.

    *slug* comes from :func:`workspace_slug`; passing a raw name here would put whatever
    it contains into a provider key.
    """
    if not slug or _SLUG_SAFE.search(slug):
        raise ValueError(f"{slug!r} is not a share-safe workspace slug; "
                         "build it with workspace_slug()")
    return f"{slug}.{WORKSPACE}{_SUFFIX}"


def parse_workspace_archive_name(basename: str):
    """Read the workspace slug out of *basename*, or ``None`` if it is not one.

    The mirror of :func:`parse_archive_name`, and refuses a separator for the same
    reason: what comes back is used as a name, so a key that slipped through instead of
    a base name would carry a path into it.
    """
    if not basename.endswith(_SUFFIX):
        return None
    if "/" in basename or "\\" in basename:
        return None
    stem = basename[: -len(_SUFFIX)]
    token = f".{WORKSPACE}"
    if not stem.endswith(token):
        return None
    slug = stem[: -len(token)]
    return slug or None


def is_share_archive_name(basename: str) -> bool:
    """Whether *basename* is an archive this system put on the share, of either kind.

    What a provider listing keeps. A share is somebody's storage and holds other things;
    an object that is neither a campaign nor a workspace archive is not ours to report,
    and a listing that guessed would offer an import of a file nothing here wrote.
    """
    return (parse_archive_name(basename) is not None
            or parse_workspace_archive_name(basename) is not None)
