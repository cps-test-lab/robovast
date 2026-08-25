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

from robovast.common.execution import is_campaign_dir

__all__ = ["RAW", "POSTPROCESSED", "VARIANTS", "archive_name", "parse_archive_name",
           "campaign_variant"]

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


def campaign_variant(campaign_root) -> str:
    """Which variant a campaign *directory* would be archived as.

    Postprocessing's own output is what tells them apart: ``_execution/data.db`` is
    written by it and by nothing else, so its presence is the question already
    answered. Reading the directory beats threading a flag down from whoever
    happened to know -- the campaign-end upload and a later ``vast share export``
    then agree without having to be told.
    """
    return POSTPROCESSED if (Path(campaign_root) / "_execution" / "data.db").exists() else RAW
