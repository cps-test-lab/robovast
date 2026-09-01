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

"""How a campaign archive is named on a share, and how that name is read back.

The name carries the *variant* because a share holds both kinds and they are not
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

from pathlib import Path

#: Postprocessing's provenance record, campaign-relative. Written by
#: ``results_processing.postprocessing`` and by nothing else, at the end of the
#: command run, listing one entry per derived output with its sources and plugin.
from robovast.common.campaign_data import POSTPROCESSING_RECORD

from robovast.common.execution import is_campaign_dir

__all__ = ["RAW", "POSTPROCESSED", "VARIANTS", "POSTPROCESSING_RECORD", "archive_name",
           "parse_archive_name", "campaign_variant", "variant_from_record"]

RAW = "raw"
POSTPROCESSED = "postprocessed"

#: Longest first, so ``.postprocessed`` is never read as an unsuffixed name.
VARIANTS = (POSTPROCESSED, RAW)

_SUFFIX = ".tar.gz"


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
